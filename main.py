from __future__ import annotations

from pathlib import Path
from time import perf_counter

from fastapi.encoders import jsonable_encoder
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from DataAnalyze.agents.executor import ExecutorAgent
from DataAnalyze.agents.perfetto_agent import PerfettoAgent
from DataAnalyze.agents.perfetto_executor import PerfettoExecutorAgent
from DataAnalyze.agents.perfetto_reviewer import PerfettoReviewer
from DataAnalyze.agents.reviewer import ReviewerAgent
from DataAnalyze.core.memory import MemoryManager
from DataAnalyze.core.workflow import WorkflowEngine
from DataAnalyze.middleware.monitor import ConsoleMonitor
from DataAnalyze.middleware.metrics import build_metrics_payload, observe_http_request
from DataAnalyze.schemas.models import (
    AgentResponse,
    ChatRequest,
    PerfettoAgentRequest,
    PerfettoAgentResponse,
    PerfettoAnalyzeRequest,
    PerfettoAnalyzeResponse,
    PerfettoQueryRequest,
    SessionMemoryItem,
    SessionMemoryResponse,
)
from DataAnalyze.tools.db.db_tool import DatabaseTool
from DataAnalyze.tools.perfetto.perfetto_sources import TraceProcessorPerfettoSource
from DataAnalyze.tools.perfetto.perfetto_tool import PerfettoTool


app = FastAPI(title="DataAnalyze API", version="0.1.0")
WEB_ROOT = Path(__file__).resolve().parent / "web"
DEBUG_PAGE = WEB_ROOT / "index.html"
PERFETTO_DEBUG_PAGE = WEB_ROOT / "perfetto.html"

memory_manager = MemoryManager()
db_tool = DatabaseTool()
perfetto_tool = PerfettoTool()
perfetto_source = TraceProcessorPerfettoSource(tool=perfetto_tool)
perfetto_executor = PerfettoExecutorAgent(perfetto_tool=perfetto_tool)
perfetto_agent = PerfettoAgent(
    data_source=perfetto_source,
    reviewer=PerfettoReviewer(),
    executor=perfetto_executor,
)
monitor = ConsoleMonitor()
executor = ExecutorAgent(db_tool=db_tool, memory_manager=memory_manager, monitor=monitor)
reviewer = ReviewerAgent()
workflow = WorkflowEngine(
    executor=executor,
    reviewer=reviewer,
    memory_manager=memory_manager,
    max_retries=2,
)


@app.middleware("http")
async def record_http_metrics(request: Request, call_next):
    start = perf_counter()
    response = None
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        duration = perf_counter() - start
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        observe_http_request(
            method=request.method,
            path=path,
            status=status_code,
            duration_sec=duration,
        )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/schema")
def schema() -> dict:
    return {"schema": db_tool.get_schema()}


@app.get("/perfetto/schema")
def perfetto_schema() -> dict:
    return {
        "trace_path": str(perfetto_tool.trace_path),
        "trace_processor_path": (
            str(perfetto_tool.trace_processor_path)
            if perfetto_tool.trace_processor_path
            else ""
        ),
        "schema": perfetto_tool.get_schema(),
    }


@app.post("/perfetto/query")
def perfetto_query(request: PerfettoQueryRequest) -> dict:
    try:
        return perfetto_source.execute_sql(request.sql)
    except Exception as ex:
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "source_type": perfetto_source.source_type,
                "dataset_id": perfetto_source.dataset_id,
                "error_reason": str(ex),
            },
        )


@app.post("/perfetto/analyze", response_model=PerfettoAnalyzeResponse)
def perfetto_analyze(request: PerfettoAnalyzeRequest) -> PerfettoAnalyzeResponse:
    try:
        return perfetto_source.analyze_problem(
            problem=request.problem,
            threshold_ms=request.threshold_ms,
            limit=request.limit,
        )
    except Exception as ex:
        fallback = PerfettoAnalyzeResponse(
            problem=request.problem,
            analysis_type="",
            sql="",
            metrics=[],
            evidence=[],
            conclusion="",
            recommendations=[],
            columns=[],
            rows=[],
        )
        payload = jsonable_encoder(fallback)
        payload["success"] = False
        payload["source_type"] = perfetto_source.source_type
        payload["dataset_id"] = perfetto_source.dataset_id
        payload["error_reason"] = str(ex)
        return JSONResponse(status_code=400, content=payload)


@app.post("/perfetto/agent", response_model=PerfettoAgentResponse)
def perfetto_agent_endpoint(request: PerfettoAgentRequest) -> PerfettoAgentResponse:
    return perfetto_agent.run(request)


@app.get("/")
def root() -> dict:
    return {"message": "DataAnalyze API is running", "debug": "/debug"}


@app.get("/metrics")
def metrics() -> Response:
    payload, content_type = build_metrics_payload()
    return Response(content=payload, media_type=content_type)


@app.get("/debug")
def debug_page() -> FileResponse:
    return FileResponse(DEBUG_PAGE)


@app.get("/perfetto/debug")
def perfetto_debug_page() -> FileResponse:
    return FileResponse(PERFETTO_DEBUG_PAGE)


@app.get("/sessions/{session_id}/memory", response_model=SessionMemoryResponse)
def get_session_memory(
    session_id: str,
    limit: int = Query(200, ge=1, le=5000),
    layer: str = Query("", description="可选: L0_RAW/L1_SUMMARY/L2_FACT"),
    include_compressed: bool = Query(True),
) -> SessionMemoryResponse:
    normalized_layer = layer.strip().upper() if layer else ""
    layer_filter = normalized_layer if normalized_layer in {"L0_RAW", "L1_SUMMARY", "L2_FACT"} else None
    records = memory_manager.get_session_memory(
        session_id=session_id,
        limit=limit,
        layer=layer_filter,
        include_compressed=include_compressed,
    )
    items = [
        SessionMemoryItem(
            id=record.db_id,
            role=record.role,
            memory_layer=record.memory_layer,
            memory_type=record.memory_type,
            content=record.content,
            compressed=record.compressed,
            salience_score=record.salience_score,
            source_range=list(record.source_range) if record.source_range else None,
            created_at=record.created_at,
        )
        for record in records
    ]
    return SessionMemoryResponse(session_id=session_id, total=len(items), items=items)


@app.post("/chat", response_model=AgentResponse)
def chat(request: ChatRequest) -> AgentResponse:
    try:
        result = workflow.process(
            session_id=request.session_id,
            query=request.query,
            max_retries=request.max_retries,
        )
        return result
    except Exception as ex:
        fallback = AgentResponse(
            success=False,
            text_reply="系统异常，请稍后重试。",
            chart=None,
            sql=None,
            columns=[],
            rows=[],
            tool_calls=[],
            workflow_events=[],
            error_reason=str(ex),
            state="FAILED",
            retry_count=0,
        )
        payload = jsonable_encoder(fallback)
        return JSONResponse(status_code=500, content=payload)
