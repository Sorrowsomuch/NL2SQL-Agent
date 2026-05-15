from __future__ import annotations

import json
from time import perf_counter
from typing import Any, Dict, List, Optional

from DataAnalyze.config import PERFETTO_LLM_CONFIG
from DataAnalyze.schemas.models import (
    PerfettoAgentRequest,
    PerfettoAgentResponse,
    PerfettoMetric,
    SchemaSelectionResult,
)
from DataAnalyze.tools.llm_tool import LLMClient, LLMEndpointConfig
from DataAnalyze.tools.perfetto.perfetto_tool import PerfettoTool


class PerfettoExecutorAgent:
    """Perfetto-specific executor for the double-agent chain.

    It mirrors the existing database executor shape, but swaps DatabaseTool for
    PerfettoTool: select a schema context, ask LLM for Perfetto SQL, guard it,
    execute through trace processor, then ask LLM to summarize the rows.
    """

    def __init__(
        self,
        perfetto_tool: PerfettoTool,
        llm_client: Optional[LLMClient] = None,
        llm_enabled: Optional[bool] = None,
    ) -> None:
        self.perfetto_tool = perfetto_tool
        self.llm_enabled = PERFETTO_LLM_CONFIG.enabled if llm_enabled is None else llm_enabled
        self.llm_client = llm_client or LLMClient(
            LLMEndpointConfig(
                base_url=PERFETTO_LLM_CONFIG.base_url,
                api_key=PERFETTO_LLM_CONFIG.api_key,
                model=PERFETTO_LLM_CONFIG.model,
                timeout_sec=PERFETTO_LLM_CONFIG.timeout_sec,
            )
        )

    def is_enabled(self) -> bool:
        return bool(self.llm_enabled and self.llm_client.is_enabled())

    def run(
        self,
        request: PerfettoAgentRequest,
        dataset_id: str,
        source_type: str,
    ) -> PerfettoAgentResponse:
        if not self.is_enabled():
            raise RuntimeError(
                "Perfetto LLM is not enabled; set DATAANALYZE_PERFETTO_LLM_ENABLED=true "
                "and DATAANALYZE_PERFETTO_LLM_API_KEY"
            )

        tool_calls: List[Dict[str, Any]] = []
        schema_context = self._select_schema_context(request, tool_calls)
        sql_payload = self._generate_sql(request, schema_context, tool_calls)
        sql = str(sql_payload.get("sql", "")).strip()
        analysis_type = str(sql_payload.get("analysis_type", "llm_perfetto_sql")).strip()
        plan = {
            "analysis_mode": request.analysis_mode,
            "analysis_type": analysis_type,
            "threshold_ms": request.threshold_ms,
            "limit": request.limit,
            "source_type": source_type,
            "dataset_id": dataset_id,
            "planner_strategy": "llm",
            "sql_generation_reason": str(sql_payload.get("reason", "")),
            "selected_tables": schema_context.selected_tables,
        }

        is_valid, guard_reason = self.perfetto_tool.validate_sql(sql)
        tool_calls.append(
            {
                "tool_name": "validate_perfetto_sql",
                "arguments": {"sql": sql, "source": "llm"},
                "success": is_valid,
                "output_preview": guard_reason,
                "duration_ms": 0.0,
            }
        )
        if not is_valid:
            raise ValueError(guard_reason)

        query_result = self._execute_sql(sql, tool_calls)
        rows = list(query_result.get("rows", []))
        columns = list(query_result.get("columns", []))
        summary = self._summarize_result(
            request=request,
            plan=plan,
            sql=sql,
            columns=columns,
            rows=rows,
            tool_calls=tool_calls,
        )

        return PerfettoAgentResponse(
            success=True,
            dataset_id=dataset_id,
            trace_id=request.trace_id,
            source_type=source_type,  # type: ignore[arg-type]
            problem=request.problem,
            analysis_type=analysis_type,
            plan=plan,
            sql=sql,
            metrics=summary["metrics"],
            evidence=summary["evidence"],
            conclusion=summary["conclusion"],
            recommendations=summary["recommendations"],
            tool_calls=tool_calls,
        )

    def _select_schema_context(
        self,
        request: PerfettoAgentRequest,
        tool_calls: List[Dict[str, Any]],
    ) -> SchemaSelectionResult:
        start = perf_counter()
        schema_context = self.perfetto_tool.select_schema_context(
            user_query=request.problem,
            max_tables=8,
            max_columns_per_table=12,
        )
        tool_calls.append(
            {
                "tool_name": "select_perfetto_schema_context",
                "arguments": {
                    "strategy": schema_context.strategy,
                    "metadata_source": schema_context.metadata_source,
                    "selected_tables": schema_context.selected_tables,
                    "selected_columns_by_table": schema_context.selected_columns_by_table,
                    "prompt_budget_chars": schema_context.prompt_budget_chars,
                },
                "success": True,
                "output_preview": schema_context.prompt_text,
                "duration_ms": round((perf_counter() - start) * 1000.0, 3),
            }
        )
        return schema_context

    def _generate_sql(
        self,
        request: PerfettoAgentRequest,
        schema_context: SchemaSelectionResult,
        tool_calls: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        system_prompt = (
            "You are a senior Perfetto SQL generator. Return JSON only. "
            "The JSON shape must be {\"sql\":\"...\",\"analysis_type\":\"...\",\"reason\":\"...\"}. "
            "Generate read-only Perfetto SQL for trace processor. The final statement must be SELECT or WITH. "
            "Only use tables and columns present in the provided schema. "
            "Durations and timestamps are nanoseconds; divide by 1e6 when returning milliseconds. "
            "Always include a LIMIT no greater than the requested limit unless the query returns a single aggregate row. "
            "Do not include SQL comments, INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, ATTACH, or DETACH."
        )
        user_prompt = (
            f"Problem: {request.problem}\n"
            f"Requested threshold_ms: {request.threshold_ms}\n"
            f"Requested limit: {request.limit}\n"
            f"Selected tables: {schema_context.selected_tables}\n"
            f"Perfetto schema:\n{schema_context.prompt_text}\n"
            "Useful join hints:\n"
            "- slice.track_id = thread_track.id\n"
            "- thread_track.utid = thread.utid\n"
            "- thread.upid = process.upid\n"
            "- sched.utid = thread.utid\n"
            "- counter.track_id = counter_track.id\n"
        )
        start = perf_counter()
        try:
            result = self.llm_client.chat_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.1,
                max_tokens=1200,
            )
            tool_calls.append(
                {
                    "tool_name": "generate_perfetto_sql",
                    "arguments": {
                        "model": PERFETTO_LLM_CONFIG.model,
                        "selected_tables": schema_context.selected_tables,
                        "prompt_debug": {
                            "problem": request.problem,
                            "threshold_ms": request.threshold_ms,
                            "limit": request.limit,
                            "schema_prompt": schema_context.prompt_text,
                        },
                        # LLMClient.chat_json returns the parsed model JSON.
                        # Keeping it here makes the frontend debug page show
                        # exactly what the model decided before guard/execution.
                        "llm_response": result,
                    },
                    "success": True,
                    "output_preview": (
                        f"analysis_type={result.get('analysis_type', '')}; "
                        f"reason={str(result.get('reason', ''))[:240]}"
                    ),
                    "duration_ms": round((perf_counter() - start) * 1000.0, 3),
                }
            )
            return result
        except Exception as ex:
            tool_calls.append(
                {
                    "tool_name": "generate_perfetto_sql",
                    "arguments": {"model": PERFETTO_LLM_CONFIG.model},
                    "success": False,
                    "output_preview": str(ex),
                    "duration_ms": round((perf_counter() - start) * 1000.0, 3),
                }
            )
            raise

    def _execute_sql(self, sql: str, tool_calls: List[Dict[str, Any]]) -> Dict[str, Any]:
        start = perf_counter()
        try:
            result = self.perfetto_tool.execute_sql(sql)
            tool_calls.append(
                {
                    "tool_name": "execute_perfetto_sql",
                    "arguments": {"sql": sql},
                    "success": True,
                    "output_preview": (
                        f"rows={result.get('row_count', 0)}; "
                        f"columns={result.get('columns', [])}"
                    ),
                    "duration_ms": float(
                        result.get("duration_ms", round((perf_counter() - start) * 1000.0, 3))
                    ),
                }
            )
            return result
        except Exception as ex:
            tool_calls.append(
                {
                    "tool_name": "execute_perfetto_sql",
                    "arguments": {"sql": sql},
                    "success": False,
                    "output_preview": str(ex),
                    "duration_ms": round((perf_counter() - start) * 1000.0, 3),
                }
            )
            raise

    def _summarize_result(
        self,
        request: PerfettoAgentRequest,
        plan: Dict[str, Any],
        sql: str,
        columns: List[str],
        rows: List[Dict[str, Any]],
        tool_calls: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        system_prompt = (
            "You are a performance analysis executor summarizing Perfetto SQL results. "
            "Return JSON only with this shape: "
            "{\"metrics\":[{\"name\":\"...\",\"value\":0,\"unit\":\"ms\",\"interpretation\":\"...\"}],"
            "\"evidence\":[\"...\"],\"conclusion\":\"...\",\"recommendations\":[\"...\"]}. "
            "Only use facts present in the rows. Do not invent values. "
            "If rows are empty, say the query returned no evidence and avoid claiming an issue."
        )
        preview_rows = rows[: min(len(rows), 40)]
        user_prompt = (
            f"Problem: {request.problem}\n"
            f"Plan: {json.dumps(plan, ensure_ascii=False, default=str)}\n"
            f"SQL: {sql}\n"
            f"Columns: {columns}\n"
            f"Row count: {len(rows)}\n"
            f"Rows sample JSON: {json.dumps(preview_rows, ensure_ascii=False, default=str)}"
        )
        start = perf_counter()
        try:
            result = self.llm_client.chat_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.2,
                max_tokens=1600,
            )
            summary = self._normalize_summary(result, rows)
            tool_calls.append(
                {
                    "tool_name": "summarize_perfetto_result",
                    "arguments": {
                        "model": PERFETTO_LLM_CONFIG.model,
                        "row_count": len(rows),
                        "columns": columns,
                        "rows_sample": preview_rows[:5],
                        "llm_response": result,
                        "normalized_summary": {
                            "metrics": [
                                metric.model_dump() if hasattr(metric, "model_dump") else metric.dict()
                                for metric in summary["metrics"]
                            ],
                            "evidence": summary["evidence"],
                            "conclusion": summary["conclusion"],
                            "recommendations": summary["recommendations"],
                        },
                    },
                    "success": True,
                    "output_preview": summary["conclusion"][:300],
                    "duration_ms": round((perf_counter() - start) * 1000.0, 3),
                }
            )
            return summary
        except Exception as ex:
            summary = self._fallback_summary(rows)
            tool_calls.append(
                {
                    "tool_name": "summarize_perfetto_result",
                    "arguments": {"model": PERFETTO_LLM_CONFIG.model, "row_count": len(rows)},
                    "success": False,
                    "output_preview": f"{ex}; fallback summary used",
                    "duration_ms": round((perf_counter() - start) * 1000.0, 3),
                }
            )
            return summary

    def _normalize_summary(self, raw: Dict[str, Any], rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        metrics: List[PerfettoMetric] = []
        for item in raw.get("metrics") or []:
            if not isinstance(item, dict):
                continue
            metrics.append(
                PerfettoMetric(
                    name=str(item.get("name", "metric")),
                    value=item.get("value", ""),
                    unit=str(item.get("unit", "")),
                    interpretation=str(item.get("interpretation", "")),
                )
            )
        if not metrics:
            metrics = self._fallback_metrics(rows)

        evidence = [str(item) for item in (raw.get("evidence") or [])][:10]
        if not evidence:
            evidence = self._fallback_evidence(rows)

        conclusion = str(raw.get("conclusion", "")).strip()
        if not conclusion:
            conclusion = self._fallback_conclusion(rows)

        recommendations = [str(item) for item in (raw.get("recommendations") or [])][:8]
        if not recommendations:
            recommendations = [
                "Narrow the query by process or thread if the trace contains multiple apps.",
                "Compare this result with adjacent slices or scheduler rows before making a root-cause claim.",
            ]

        return {
            "metrics": metrics,
            "evidence": evidence,
            "conclusion": conclusion,
            "recommendations": recommendations,
        }

    def _fallback_summary(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "metrics": self._fallback_metrics(rows),
            "evidence": self._fallback_evidence(rows),
            "conclusion": self._fallback_conclusion(rows),
            "recommendations": [
                "Inspect the SQL and top returned rows to confirm the LLM query matched the performance question.",
                "If the result is too broad, add process/thread filters or a tighter time window.",
            ],
        }

    @staticmethod
    def _fallback_metrics(rows: List[Dict[str, Any]]) -> List[PerfettoMetric]:
        metrics = [PerfettoMetric(name="row_count", value=len(rows), unit="rows")]
        if rows:
            first = rows[0]
            for key in ("dur_ms", "duration_ms", "cpu_time_ms"):
                if key in first:
                    metrics.append(
                        PerfettoMetric(
                            name=f"top_{key}",
                            value=first.get(key),
                            unit="ms",
                            interpretation="Top returned row metric from the LLM-generated query.",
                        )
                    )
                    break
        return metrics

    @staticmethod
    def _fallback_evidence(rows: List[Dict[str, Any]]) -> List[str]:
        if not rows:
            return []
        return [json.dumps(row, ensure_ascii=False, default=str) for row in rows[:5]]

    @staticmethod
    def _fallback_conclusion(rows: List[Dict[str, Any]]) -> str:
        if not rows:
            return "The LLM-generated Perfetto query returned no rows, so there is no evidence for this issue from that query."
        return "The LLM-generated Perfetto query returned rows; inspect the metrics and evidence before drawing a root-cause conclusion."
