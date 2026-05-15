from __future__ import annotations

from time import perf_counter
from typing import Optional

from DataAnalyze.agents.perfetto_executor import PerfettoExecutorAgent
from DataAnalyze.agents.perfetto_reviewer import PerfettoReviewer
from DataAnalyze.schemas.models import (
    PerfettoAgentRequest,
    PerfettoAgentResponse,
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
        executor: Optional[PerfettoExecutorAgent] = None,
    ) -> None:
        self.data_source = data_source
        self.reviewer = reviewer or PerfettoReviewer()
        self.executor = executor

    def run(self, request: PerfettoAgentRequest) -> PerfettoAgentResponse:
        dataset_id = request.dataset_id or self.data_source.dataset_id
        source_type = self.data_source.source_type
        if request.analysis_mode.strip().lower() == "llm":
            if self.executor is None:
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
                        "planner_strategy": "llm",
                    },
                    error_reason="Perfetto LLM executor is not configured",
                )
                response.review = self.reviewer.run(response)
                return response
            try:
                response = self.executor.run(
                    request=request,
                    dataset_id=dataset_id,
                    source_type=source_type,
                )
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
                        "planner_strategy": "llm",
                    },
                    error_reason=str(ex),
                )
            response.review = self.reviewer.run(response)
            return response

        plan = build_plan_from_problem(
            problem=request.problem,
            threshold_ms=request.threshold_ms,
            limit=request.limit,
            analysis_mode=request.analysis_mode,
            dataset_id=dataset_id,
            source_type=source_type,
        )
        start = perf_counter()
        try:
            # v1 keeps SQL generation inside deterministic templates. Later this
            # call can route to a database-backed source without changing the UI.
            analysis = self.data_source.analyze_problem(
                problem=request.problem,
                threshold_ms=request.threshold_ms,
                limit=request.limit,
            )
            tool_calls = [
                {
                    "tool_name": "perfetto_analyze",
                    "arguments": {
                        "dataset_id": dataset_id,
                        "source_type": source_type,
                        "plan": plan,
                    },
                    "success": True,
                    "output_preview": f"analysis_type={analysis.analysis_type}; rows={len(analysis.rows)}",
                    "duration_ms": round((perf_counter() - start) * 1000.0, 3),
                }
            ]
            response = PerfettoAgentResponse(
                success=True,
                dataset_id=dataset_id,
                trace_id=request.trace_id,
                source_type=source_type,
                problem=request.problem,
                analysis_type=analysis.analysis_type,
                plan={**plan, "analysis_type": analysis.analysis_type},
                sql=analysis.sql,
                metrics=analysis.metrics,
                evidence=analysis.evidence,
                conclusion=analysis.conclusion,
                recommendations=analysis.recommendations,
                tool_calls=tool_calls,
            )
        except Exception as ex:
            response = PerfettoAgentResponse(
                success=False,
                dataset_id=dataset_id,
                trace_id=request.trace_id,
                source_type=source_type,
                problem=request.problem,
                analysis_type=str(plan.get("analysis_type", "")),
                plan=plan,
                tool_calls=[
                    {
                        "tool_name": "perfetto_analyze",
                        "arguments": {
                            "dataset_id": dataset_id,
                            "source_type": source_type,
                            "plan": plan,
                        },
                        "success": False,
                        "output_preview": str(ex),
                        "duration_ms": round((perf_counter() - start) * 1000.0, 3),
                    }
                ],
                error_reason=str(ex),
            )

        response.review = self.reviewer.run(response)
        return response
