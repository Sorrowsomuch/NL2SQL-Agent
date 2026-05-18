from __future__ import annotations

from time import perf_counter
from typing import Any, Dict, List, Optional

from DataAnalyze.agents.perfetto_reviewer import PerfettoReviewer
from DataAnalyze.agents.executor import ExecutorAgent
from DataAnalyze.agents.reviewer import ReviewerAgent
from DataAnalyze.core.workflow import WorkflowEngine
from DataAnalyze.schemas.models import (
    AgentResponse,
    PerfettoAgentRequest,
    PerfettoAgentResponse,
    PerfettoMetric,
    PerfettoReviewResult,
)
from DataAnalyze.tools.perfetto.perfetto_sources import PerfettoDataSource
from DataAnalyze.tools.perfetto.perfetto_templates import build_plan_from_problem


class PerfettoAgent:
    """Frontend-facing Perfetto analysis agent facade.

    This is intentionally thin: it preserves the agent-shaped contract for UI
    integration while delegating source access, SQL templating, and review.
    """

    def __init__(
        self,
        data_source: PerfettoDataSource,
        reviewer: Optional[PerfettoReviewer] = None,
        executor: Optional[ExecutorAgent] = None,
        reviewer_agent: Optional[ReviewerAgent] = None,
        workflow: Optional[WorkflowEngine] = None,
    ) -> None:
        self.data_source = data_source
        self.reviewer = reviewer or PerfettoReviewer()
        self.executor = executor
        self.reviewer_agent = reviewer_agent
        self.workflow = workflow

    def run(self, request: PerfettoAgentRequest) -> PerfettoAgentResponse:
        dataset_id = request.dataset_id or self.data_source.dataset_id
        source_type = self.data_source.source_type
        analysis_mode = request.analysis_mode.strip().lower()
        if analysis_mode != "llm":
            response = PerfettoAgentResponse(
                success=False,
                dataset_id=dataset_id,
                trace_id=request.trace_id,
                source_type=source_type,
                problem=request.problem,
                analysis_type=analysis_mode,
                plan={
                    "analysis_mode": request.analysis_mode,
                    "source_type": source_type,
                    "dataset_id": dataset_id,
                    "planner_strategy": "workflow_engine",
                },
                error_reason="POST /perfetto/agent only supports analysis_mode=llm",
            )
            response.review = self.reviewer.run(response)
            return response

        if self.workflow is None:
            response = PerfettoAgentResponse(
                success=False,
                dataset_id=dataset_id,
                trace_id=request.trace_id,
                source_type=source_type,
                problem=request.problem,
                analysis_type="llm",
                plan={
                    "analysis_mode": request.analysis_mode,
                    "source_type": source_type,
                    "dataset_id": dataset_id,
                    "planner_strategy": "workflow_engine",
                },
                error_reason="Perfetto workflow is not configured",
            )
            response.review = self.reviewer.run(response)
            return response

        try:
            agent_response = self.workflow.process(
                session_id=request.session_id,
                query=request.problem,
            )
            response = self._from_agent_response(
                agent_response=agent_response,
                request=request,
                dataset_id=dataset_id,
                source_type=source_type,
            )
            response.review = PerfettoReviewResult(
                approved=agent_response.success,
                reason=agent_response.review_reason or agent_response.error_reason or "",
            )
            return response
        except Exception as ex:
            response = PerfettoAgentResponse(
                success=False,
                dataset_id=dataset_id,
                trace_id=request.trace_id,
                source_type=source_type,
                problem=request.problem,
                analysis_type="llm",
                plan={
                    "analysis_mode": request.analysis_mode,
                    "source_type": source_type,
                    "dataset_id": dataset_id,
                    "planner_strategy": "workflow_engine",
                },
                error_reason=str(ex),
            )
            response.review = self.reviewer.run(response)
            return response

    def _from_agent_response(
        self,
        agent_response: AgentResponse,
        request: PerfettoAgentRequest,
        dataset_id: str,
        source_type: str,
    ) -> PerfettoAgentResponse:
        plan = self._build_plan_from_tool_calls(
            tool_calls=agent_response.tool_calls,
            request=request,
            dataset_id=dataset_id,
            source_type=source_type,
        )
        if agent_response.workflow_events:
            plan["workflow_events"] = [
                item.model_dump() if hasattr(item, "model_dump") else dict(item)
                for item in agent_response.workflow_events
            ]
        if agent_response.review_debug:
            plan["review_debug"] = agent_response.review_debug
        if agent_response.review_reason:
            plan["review_reason"] = agent_response.review_reason
        plan["workflow_state"] = agent_response.state
        plan["retry_count"] = agent_response.retry_count
        metrics = self._derive_metrics(agent_response.rows)
        evidence = list(agent_response.professional_findings[:8])
        if not evidence:
            evidence = self._rows_as_evidence(agent_response.rows)
        return PerfettoAgentResponse(
            success=agent_response.success,
            dataset_id=dataset_id,
            trace_id=request.trace_id,
            source_type=source_type,
            problem=request.problem,
            analysis_type=str(plan.get("analysis_type", "llm")),
            plan=plan,
            sql=agent_response.sql or "",
            metrics=metrics,
            evidence=evidence,
            conclusion=agent_response.text_reply,
            recommendations=list(agent_response.recommendations[:8]),
            tool_calls=[
                item.model_dump() if hasattr(item, "model_dump") else dict(item)
                for item in agent_response.tool_calls
            ],
            error_reason=agent_response.error_reason,
        )

    def _build_plan_from_tool_calls(
        self,
        tool_calls: List[Any],
        request: PerfettoAgentRequest,
        dataset_id: str,
        source_type: str,
    ) -> Dict[str, Any]:
        plan: Dict[str, Any] = {
            "analysis_mode": request.analysis_mode,
            "analysis_type": "llm",
            "threshold_ms": request.threshold_ms,
            "limit": request.limit,
            "source_type": source_type,
            "dataset_id": dataset_id,
            "planner_strategy": "executor_agent",
        }
        for call in tool_calls:
            tool_name = getattr(call, "tool_name", "")
            arguments = getattr(call, "arguments", {}) or {}
            if tool_name == "select_schema_context":
                plan.update(
                    {
                        "analysis_type": arguments.get("query_planner_primary_metric") or "llm",
                        "schema_strategy": arguments.get("strategy"),
                        "selected_tables": arguments.get("selected_tables"),
                        "selected_relationships": arguments.get("selected_relationships"),
                        "knowledge_hit_ids": arguments.get("knowledge_hit_ids"),
                        "knowledge_column_hints": arguments.get("knowledge_column_hints"),
                    }
                )
            if tool_name == "validate_generated_sql":
                plan["sql_guard"] = {
                    "outcome": arguments.get("outcome"),
                    "reason": arguments.get("reason"),
                    "final_sql_source": arguments.get("final_sql_source"),
                }
        return plan

    def _derive_metrics(self, rows: List[Dict[str, Any]]) -> List[PerfettoMetric]:
        if not rows:
            return [PerfettoMetric(name="row_count", value=0, unit="rows", interpretation="未命中结果")]
        metrics: List[PerfettoMetric] = [
            PerfettoMetric(name="row_count", value=len(rows), unit="rows", interpretation="SQL 返回行数")
        ]
        first = rows[0]
        for key, value in first.items():
            lowered = str(key).lower()
            if isinstance(value, (int, float)) and lowered.endswith("_ms"):
                metrics.append(
                    PerfettoMetric(
                        name=str(key),
                        value=value,
                        unit="ms",
                        interpretation="首行毫秒级性能指标",
                    )
                )
                break
        return metrics

    def _rows_as_evidence(self, rows: List[Dict[str, Any]]) -> List[str]:
        evidence: List[str] = []
        for index, row in enumerate(rows[:5], start=1):
            evidence.append(f"row {index}: {row}")
        return evidence
