from __future__ import annotations

import os
import re
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, Iterable, List, Optional

from DataAnalyze.config import EXECUTOR_LLM_CONFIG
from DataAnalyze.middleware.metrics import observe_column_planner, observe_query_planner
from DataAnalyze.schemas.models import (
    DatabaseSchemaMetadata,
    PerfettoAnalyzeResponse,
    PerfettoMetric,
    SQLExecutionResult,
    SchemaColumn,
    SchemaRelationship,
    SchemaSelectionResult,
    TableSchema,
)
from DataAnalyze.tools.db.field_planner import (
    build_final_column_priorities,
    build_planner_prompt_context,
    sanitize_planner_output,
)
from DataAnalyze.tools.db.knowledge_retrieval import KnowledgeRetriever, KnowledgeRetrievalResult
from DataAnalyze.tools.db.query_planner import (
    build_query_plan_summary,
    build_query_planner_prompt_context,
    merge_query_planner_tables,
    sanitize_query_plan_output,
)
from DataAnalyze.tools.llm_tool import LLMClient, LLMEndpointConfig
from DataAnalyze.tools.perfetto.perfetto_templates import (
    build_plan_from_problem,
    build_sql_from_plan,
    summarize_result,
)


class PerfettoTool:
    """Read-only query tool for Perfetto trace files such as output.pb."""

    _DANGEROUS_KEYWORDS = re.compile(
        r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|ATTACH|DETACH)\b",
        flags=re.IGNORECASE,
    )
    _INCLUDE_RE = re.compile(
        r"^INCLUDE\s+PERFETTO\s+MODULE\s+[A-Za-z0-9_.]+$",
        flags=re.IGNORECASE,
    )

    def __init__(self, trace_path: Optional[str] = None) -> None:
        # perfetto_tool.py now lives in DataAnalyze/tools/perfetto/, but the
        # default trace fixture is still the repository-root output.pb.
        root = Path(__file__).resolve().parents[3]
        configured_path = trace_path or os.getenv("DATAANALYZE_PERFETTO_TRACE_PATH")
        self.trace_path = Path(configured_path).expanduser() if configured_path else root / "output.pb"
        if not self.trace_path.is_absolute():
            self.trace_path = root / self.trace_path
        configured_shell_path = os.getenv("DATAANALYZE_PERFETTO_TRACE_PROCESSOR_PATH", "")
        self.trace_processor_path = (
            Path(configured_shell_path).expanduser() if configured_shell_path.strip() else None
        )
        self.knowledge_retriever = KnowledgeRetriever()
        self.query_planner_client = LLMClient(
            LLMEndpointConfig(
                base_url=EXECUTOR_LLM_CONFIG.base_url,
                api_key=EXECUTOR_LLM_CONFIG.api_key,
                model=EXECUTOR_LLM_CONFIG.model,
                timeout_sec=EXECUTOR_LLM_CONFIG.timeout_sec,
            )
        )
        self.column_planner_client = LLMClient(
            LLMEndpointConfig(
                base_url=EXECUTOR_LLM_CONFIG.base_url,
                api_key=EXECUTOR_LLM_CONFIG.api_key,
                model=EXECUTOR_LLM_CONFIG.model,
                timeout_sec=EXECUTOR_LLM_CONFIG.timeout_sec,
            )
        )
        self._processor: Optional[Any] = None

    def get_schema(self) -> str:
        return self.render_schema_prompt(self.get_schema_metadata())

    def get_schema_metadata(self) -> DatabaseSchemaMetadata:
        return DatabaseSchemaMetadata(
            source="perfetto",
            schema_name="perfetto",
            tables=self._builtin_perfetto_tables(),
            relationships=self._builtin_perfetto_relationships(),
            notes=[
                f"Perfetto trace path: {self.trace_path}",
                "Timestamps and durations are nanoseconds; divide by 1e6 for milliseconds.",
                "Use LIMIT for exploratory queries to keep tool output compact.",
            ],
        )

    def select_schema_context(
        self,
        user_query: str,
        retry_count: int = 0,
        max_tables: Optional[int] = None,
        max_columns_per_table: Optional[int] = None,
    ) -> SchemaSelectionResult:
        strategy = "perfetto-expanded" if retry_count > 0 else "perfetto-focused"
        table_limit = max_tables or (8 if retry_count > 0 else 5)
        column_limit = max_columns_per_table or (12 if retry_count > 0 else 8)
        inventory = self.get_schema_metadata()
        fetch_mode = "builtin-two-stage"
        inventory_tables = [table.name for table in inventory.tables]

        knowledge_result = self.knowledge_retriever.retrieve(
            query=user_query,
            allowed_tables=inventory_tables,
        )
        query_planner_knowledge_result = self.knowledge_retriever.retrieve(
            query=user_query,
            allowed_tables=inventory_tables,
            kinds=["table_profile", "query_pattern", "metric_definition"],
        )
        ranked_tables = self._rank_tables_for_query(
            user_query=user_query,
            metadata=inventory,
            knowledge_result=knowledge_result,
        )
        (
            query_planner_strategy,
            query_planner_reason,
            query_plan,
            query_planner_notes,
        ) = self._plan_query_with_llm(
            user_query=user_query,
            inventory=inventory,
            ranked_tables=ranked_tables,
            knowledge_hits=query_planner_knowledge_result.hits,
        )
        selected_tables = merge_query_planner_tables(
            ranked_tables=ranked_tables,
            hard_tables=query_plan["candidate_tables_hard"],
            soft_tables=query_plan["candidate_tables_soft"],
            limit=table_limit,
        )
        if not selected_tables:
            selected_tables = inventory_tables[:table_limit]

        column_knowledge_result = self.knowledge_retriever.retrieve(
            query=user_query,
            allowed_tables=selected_tables,
            kinds=["column_semantics"],
        )
        column_hits = self.knowledge_retriever.collect_column_hints(
            retrieval=column_knowledge_result,
            selected_tables=selected_tables,
            max_hits=6 if retry_count > 0 else 4,
        )
        planning_knowledge_result = self.knowledge_retriever.retrieve(
            query=user_query,
            allowed_tables=selected_tables,
            kinds=["column_semantics", "query_pattern", "metric_definition"],
        )
        query_plan_summary = build_query_plan_summary(query_plan)
        base_column_priorities = self._build_column_priorities(
            metadata=inventory,
            selected_tables=selected_tables,
            column_hits=column_hits,
            query_plan=query_plan,
        )
        (
            column_planner_strategy,
            column_planner_reason,
            planner_required_columns_by_table,
            planner_optional_columns_by_table,
            planner_notes,
        ) = self._plan_columns_with_llm(
            user_query=user_query,
            metadata=inventory,
            selected_tables=selected_tables,
            column_hits=column_hits,
            planning_hits=planning_knowledge_result.hits,
            query_plan_summary=query_plan_summary,
        )
        if planner_required_columns_by_table or planner_optional_columns_by_table:
            column_priorities = build_final_column_priorities(
                metadata=inventory,
                selected_tables=selected_tables,
                base_priorities=base_column_priorities,
                planner_required=planner_required_columns_by_table,
                planner_optional=planner_optional_columns_by_table,
            )
            column_selection_strategy = "llm-planner"
        else:
            column_priorities = base_column_priorities
            column_selection_strategy = (
                "knowledge-aware" if any(column_priorities.values()) else "perfetto-priority"
            )
        selected_schema = self._slice_schema_metadata(
            metadata=inventory,
            selected_tables=selected_tables,
            max_columns_per_table=column_limit,
            prioritized_columns_by_table=column_priorities,
        )
        knowledge_prompt = self.knowledge_retriever.build_prompt_context(
            retrieval=knowledge_result,
            selected_tables=[table.name for table in selected_schema.tables],
            max_hits=4 if retry_count > 0 else 3,
        )
        column_knowledge_prompt = self.knowledge_retriever.build_prompt_context(
            retrieval=column_knowledge_result,
            selected_tables=[table.name for table in selected_schema.tables],
            max_hits=6 if retry_count > 0 else 4,
        )
        selected_schema = DatabaseSchemaMetadata(
            source="perfetto",
            schema_name="perfetto",
            tables=selected_schema.tables,
            relationships=selected_schema.relationships,
            notes=list(selected_schema.notes),
        )
        prompt_text = self.render_schema_prompt(
            selected_schema,
            max_tables=table_limit,
            max_columns_per_table=column_limit,
        )
        if knowledge_prompt:
            prompt_text += "\n\n" + knowledge_prompt
        if column_knowledge_prompt:
            prompt_text += "\n\n" + column_knowledge_prompt
        selected_columns_by_table = {
            table.name: [column.name for column in table.columns]
            for table in selected_schema.tables
        }
        relationship_summaries = [
            f"{item.from_table}({', '.join(item.from_columns)}) -> "
            f"{item.to_table}({', '.join(item.to_columns)})"
            for item in selected_schema.relationships
        ]
        knowledge_column_hints = [
            f"{hit.table_name}.{hit.column_name}"
            for hit in column_hits
            if hit.table_name and hit.column_name
        ]
        retrieval_notes = list(selected_schema.notes)
        retrieval_notes.extend(knowledge_result.notes)
        retrieval_notes.extend(query_planner_knowledge_result.notes)
        retrieval_notes.extend(column_knowledge_result.notes)
        retrieval_notes.extend(planning_knowledge_result.notes)
        retrieval_notes.extend(query_planner_notes)
        retrieval_notes.extend(planner_notes)
        retrieval_notes.append(
            f"Query planner strategy: {query_planner_strategy}; reason={query_planner_reason}."
        )
        retrieval_notes.append(
            f"Column planner strategy: {column_planner_strategy}; reason={column_planner_reason}."
        )
        retrieval_notes.append(
            f"Column selection strategy: {column_selection_strategy}; column hints={len(knowledge_column_hints)}."
        )
        retrieval_notes.append(f"Query plan summary: {query_plan_summary or 'none'}.")
        retrieval_notes.append(
            f"Selected {len(selected_schema.tables)} Perfetto table(s), "
            f"{sum(len(table.columns) for table in selected_schema.tables)} column(s)."
        )
        query_planner_table_count = len(query_plan["candidate_tables_hard"]) + len(
            query_plan["candidate_tables_soft"]
        )
        query_planner_dimension_count = len(query_plan["analysis_dimensions"]) + len(
            query_plan["filter_dimensions"]
        )
        observe_query_planner(
            strategy=query_planner_strategy,
            outcome=(
                "success"
                if query_planner_table_count > 0
                else ("disabled" if query_planner_strategy == "disabled" else "fallback")
            ),
            candidate_table_count=query_planner_table_count,
            dimension_count=query_planner_dimension_count,
        )
        planner_field_count = sum(
            len(columns) for columns in planner_required_columns_by_table.values()
        ) + sum(len(columns) for columns in planner_optional_columns_by_table.values())
        observe_column_planner(
            strategy=column_planner_strategy,
            outcome=(
                "success"
                if planner_field_count > 0
                else ("disabled" if column_planner_strategy == "disabled" else "fallback")
            ),
            field_count=planner_field_count,
        )
        return SchemaSelectionResult(
            strategy=strategy,
            metadata_source="perfetto",
            selected_schema=selected_schema,
            selected_tables=[table.name for table in selected_schema.tables],
            selected_relationships=relationship_summaries,
            prompt_text=prompt_text,
            prompt_budget_chars=len(prompt_text),
            fetch_mode=fetch_mode,
            discovery_tables=selected_tables,
            query_planner_strategy=query_planner_strategy,
            query_planner_reason=query_planner_reason,
            query_planner_query_type=query_plan["query_type"],
            query_planner_primary_metric=query_plan["primary_metric"],
            query_planner_time_requirement=query_plan["time_requirement"],
            query_planner_analysis_dimensions=query_plan["analysis_dimensions"],
            query_planner_filter_dimensions=query_plan["filter_dimensions"],
            query_planner_candidate_tables_hard=query_plan["candidate_tables_hard"],
            query_planner_candidate_tables_soft=query_plan["candidate_tables_soft"],
            query_planner_join_needed=query_plan["join_needed"],
            column_selection_strategy=column_selection_strategy,
            selected_columns_by_table=selected_columns_by_table,
            column_planner_strategy=column_planner_strategy,
            column_planner_reason=column_planner_reason,
            planner_required_columns_by_table=planner_required_columns_by_table,
            planner_optional_columns_by_table=planner_optional_columns_by_table,
            knowledge_strategy=knowledge_result.strategy,
            knowledge_hit_ids=knowledge_result.hit_ids,
            knowledge_hit_titles=knowledge_result.hit_titles,
            knowledge_column_hints=knowledge_column_hints,
            knowledge_prompt_text="\n\n".join(
                part for part in [knowledge_prompt, column_knowledge_prompt] if part
            ),
            retrieval_notes=retrieval_notes,
        )

    def render_schema_prompt(
        self,
        metadata: DatabaseSchemaMetadata,
        include_relationships: bool = True,
        include_indexes: bool = False,
        max_tables: Optional[int] = None,
        max_columns_per_table: Optional[int] = None,
    ) -> str:
        tables = metadata.tables[: max_tables or len(metadata.tables)]
        sections: List[str] = []
        for table in tables:
            columns = table.columns[: max_columns_per_table or len(table.columns)]
            column_text = ", ".join(
                f"{column.name} {column.data_type}"
                + (f" [hint={column.semantic_hint}]" if column.semantic_hint else "")
                for column in columns
            )
            line = f"table: {table.name}({column_text})"
            notes = []
            if table.semantic_hint:
                notes.append(f"purpose={table.semantic_hint}")
            if table.row_grain:
                notes.append(f"grain={table.row_grain}")
            if notes:
                line += " {" + "; ".join(notes) + "}"
            sections.append(line)
        if include_relationships and metadata.relationships:
            sections.append("relationships:")
            for relation in metadata.relationships:
                sections.append(
                    f"- {relation.from_table}({', '.join(relation.from_columns)}) -> "
                    f"{relation.to_table}({', '.join(relation.to_columns)})"
                )
        if metadata.notes:
            sections.append("notes: " + " | ".join(metadata.notes))
        return "\n".join(sections)

    def execute_sql(self, sql: str) -> Dict[str, Any]:
        normalized = (sql or "").strip()
        is_valid, guardrail_reason = self.validate_sql(normalized)
        if not is_valid:
            raise ValueError(guardrail_reason)

        start = perf_counter()
        processor = self._get_processor()
        result = self._execute_perfetto_query(processor, normalized)
        payload = result.model_dump() if hasattr(result, "model_dump") else result.dict()
        payload["duration_ms"] = round((perf_counter() - start) * 1000.0, 3)
        return payload

    def validate_sql(self, sql: str) -> tuple[bool, str]:
        """Validate Perfetto SQL before execution.

        The LLM-based executor uses this public guard entrypoint to record a
        reviewable tool trace before it calls trace processor.
        """

        normalized = (sql or "").strip()
        if not normalized:
            return False, "SQL cannot be empty"
        guardrail_error = self._scan_sql_guardrails(normalized)
        if guardrail_error:
            return False, guardrail_error
        return True, "ok"

    def analyze_problem(
        self,
        problem: str,
        threshold_ms: float = 16.0,
        limit: int = 20,
    ) -> PerfettoAnalyzeResponse:
        plan = build_plan_from_problem(
            problem=problem,
            threshold_ms=threshold_ms,
            limit=limit,
        )
        sql = build_sql_from_plan(plan)
        result = self.execute_sql(sql)
        rows = list(result.get("rows", []))
        metrics, evidence, conclusion, recommendations = summarize_result(plan, rows)

        return PerfettoAnalyzeResponse(
            problem=problem,
            analysis_type=str(plan.get("analysis_type", "long_slice")),
            sql=sql,
            metrics=metrics,
            evidence=evidence,
            conclusion=conclusion,
            recommendations=recommendations,
            columns=list(result.get("columns", [])),
            rows=rows,
        )

    def _scan_sql_guardrails(self, sql: str) -> Optional[str]:
        masked_sql = self._mask_string_literals(sql)
        if "--" in masked_sql or "/*" in masked_sql:
            return "Security guardrail: SQL comments are not allowed"
        if self._DANGEROUS_KEYWORDS.search(masked_sql):
            return "Security guardrail: dangerous SQL keyword detected"

        statements = [part.strip() for part in masked_sql.split(";") if part.strip()]
        if not statements:
            return "SQL cannot be empty"
        for statement in statements[:-1]:
            if not self._INCLUDE_RE.match(statement):
                return "Security guardrail: only INCLUDE PERFETTO MODULE may precede the final SELECT"

        final_statement = statements[-1]
        if not re.match(r"^(SELECT|WITH)\b", final_statement, flags=re.IGNORECASE):
            return "Security guardrail: final Perfetto query must be SELECT or WITH"
        return None

    def _mask_string_literals(self, sql: str) -> str:
        masked_chars: List[str] = []
        in_single_quote = False
        in_double_quote = False
        index = 0
        while index < len(sql):
            char = sql[index]
            next_char = sql[index + 1] if index + 1 < len(sql) else ""
            if in_single_quote:
                masked_chars.append(" ")
                if char == "'" and next_char == "'":
                    masked_chars.append(" ")
                    index += 2
                    continue
                if char == "'":
                    in_single_quote = False
                index += 1
                continue
            if in_double_quote:
                masked_chars.append(" ")
                if char == '"' and next_char == '"':
                    masked_chars.append(" ")
                    index += 2
                    continue
                if char == '"':
                    in_double_quote = False
                index += 1
                continue
            if char == "'":
                in_single_quote = True
                masked_chars.append(" ")
            elif char == '"':
                in_double_quote = True
                masked_chars.append(" ")
            else:
                masked_chars.append(char)
            index += 1
        return "".join(masked_chars)

    def _get_processor(self) -> Any:
        if not self.trace_path.exists():
            raise FileNotFoundError(f"Perfetto trace file not found: {self.trace_path}")
        if self._processor is not None:
            return self._processor
        try:
            from perfetto.trace_processor import TraceProcessor, TraceProcessorConfig
        except Exception as ex:
            raise RuntimeError(
                "Perfetto Python package is unavailable; install DataAnalyze requirements first"
            ) from ex

        config = TraceProcessorConfig(
            bin_path=str(self.trace_processor_path) if self.trace_processor_path else None,
        )
        self._processor = TraceProcessor(trace=str(self.trace_path), config=config)
        return self._processor

    def _execute_perfetto_query(self, processor: Any, sql: str) -> SQLExecutionResult:
        query_result = processor.query(sql)
        rows_raw = list(query_result)
        columns = self._extract_columns(query_result, rows_raw)
        rows = [self._row_to_dict(row, columns) for row in rows_raw]
        return SQLExecutionResult(
            sql=sql,
            row_count=len(rows),
            columns=columns,
            rows=rows,
            source="perfetto",
        )

    def _extract_columns(self, query_result: Any, rows: List[Any]) -> List[str]:
        for attr in ("columns", "column_names", "col_names"):
            value = getattr(query_result, attr, None)
            if value:
                return [str(item) for item in value]
        if rows:
            first = rows[0]
            if hasattr(first, "keys"):
                return [str(item) for item in first.keys()]
            if hasattr(first, "_fields"):
                return [str(item) for item in first._fields]
            if hasattr(first, "__dict__"):
                return [key for key in first.__dict__.keys() if not key.startswith("_")]
        return []

    def _row_to_dict(self, row: Any, columns: List[str]) -> Dict[str, Any]:
        if isinstance(row, dict):
            return dict(row)
        if hasattr(row, "as_dict"):
            return dict(row.as_dict())
        if hasattr(row, "_asdict"):
            return dict(row._asdict())
        if hasattr(row, "keys"):
            return {str(key): row[key] for key in row.keys()}
        if columns:
            values = []
            for column in columns:
                values.append(getattr(row, column, None))
            return dict(zip(columns, values))
        if hasattr(row, "__dict__"):
            return {key: value for key, value in row.__dict__.items() if not key.startswith("_")}
        return {"value": row}

    def _select_analysis_type(self, problem: str) -> str:
        normalized = (problem or "").lower()
        if any(token in normalized for token in ["cpu", "sched", "调度", "占用"]):
            return "cpu_time"
        return "long_slice"

    def _build_long_slice_sql(self, threshold_ms: float, limit: int) -> str:
        threshold_ns = int(max(threshold_ms, 0.001) * 1_000_000)
        return (
            "SELECT "
            "process.name AS process_name, "
            "thread.name AS thread_name, "
            "slice.name AS slice_name, "
            "slice.ts / 1e6 AS ts_ms, "
            "slice.dur / 1e6 AS dur_ms "
            "FROM slice "
            "JOIN thread_track ON slice.track_id = thread_track.id "
            "JOIN thread USING (utid) "
            "LEFT JOIN process USING (upid) "
            f"WHERE slice.dur > {threshold_ns} "
            "ORDER BY slice.dur DESC "
            f"LIMIT {int(limit)}"
        )

    def _build_cpu_time_sql(self, limit: int) -> str:
        return (
            "SELECT "
            "process.name AS process_name, "
            "thread.name AS thread_name, "
            "SUM(sched.dur) / 1e6 AS cpu_time_ms "
            "FROM sched "
            "JOIN thread USING (utid) "
            "LEFT JOIN process USING (upid) "
            "GROUP BY process.name, thread.name "
            "ORDER BY cpu_time_ms DESC "
            f"LIMIT {int(limit)}"
        )

    def _summarize_long_slices(
        self,
        problem: str,
        rows: List[Dict[str, Any]],
        threshold_ms: float,
    ) -> tuple[List[PerfettoMetric], List[str], str, List[str]]:
        durations = [float(row.get("dur_ms") or 0.0) for row in rows]
        max_duration = max(durations) if durations else 0.0
        metrics = [
            PerfettoMetric(
                name="long_slice_count",
                value=len(rows),
                unit="count",
                interpretation=f"Number of slices longer than {threshold_ms:g} ms in the inspected top window.",
            ),
            PerfettoMetric(
                name="max_duration_ms",
                value=round(max_duration, 3),
                unit="ms",
                interpretation="Longest slice duration returned by the query.",
            ),
            PerfettoMetric(
                name="threshold_ms",
                value=threshold_ms,
                unit="ms",
                interpretation="Current long-task threshold.",
            ),
        ]
        evidence = []
        for row in rows[:5]:
            evidence.append(
                f"{row.get('process_name') or 'unknown_process'} / "
                f"{row.get('thread_name') or 'unknown_thread'} / "
                f"{row.get('slice_name') or 'unknown_slice'}: "
                f"{float(row.get('dur_ms') or 0.0):.3f} ms"
            )

        if not rows:
            conclusion = (
                f"No slice longer than {threshold_ms:g} ms was found in this trace query. "
                "The current evidence does not show an obvious long synchronous task."
            )
        else:
            top = rows[0]
            conclusion = (
                f"Found {len(rows)} slices longer than {threshold_ms:g} ms. "
                f"The longest one is {float(top.get('dur_ms') or 0.0):.3f} ms on "
                f"{top.get('process_name') or 'unknown_process'} / "
                f"{top.get('thread_name') or 'unknown_thread'}, slice="
                f"{top.get('slice_name') or 'unknown_slice'}."
            )

        recommendations = [
            "Filter the result by target process and main/UI thread to confirm user-visible impact.",
            "Inspect neighboring slices around the top timestamp to identify the blocking phase.",
            "If frame timeline tables exist, correlate long slices with frame/jank events.",
        ]
        return metrics, evidence, conclusion, recommendations

    def _summarize_cpu_time(
        self,
        problem: str,
        rows: List[Dict[str, Any]],
    ) -> tuple[List[PerfettoMetric], List[str], str, List[str]]:
        top_cpu = float(rows[0].get("cpu_time_ms") or 0.0) if rows else 0.0
        total_top_cpu = sum(float(row.get("cpu_time_ms") or 0.0) for row in rows)
        metrics = [
            PerfettoMetric(
                name="top_thread_cpu_time_ms",
                value=round(top_cpu, 3),
                unit="ms",
                interpretation="CPU scheduled time of the highest ranked thread in the returned rows.",
            ),
            PerfettoMetric(
                name="returned_threads",
                value=len(rows),
                unit="count",
                interpretation="Number of process/thread groups returned by the CPU query.",
            ),
            PerfettoMetric(
                name="top_rows_cpu_time_ms",
                value=round(total_top_cpu, 3),
                unit="ms",
                interpretation="Sum of CPU scheduled time across returned top rows.",
            ),
        ]
        evidence = []
        for row in rows[:5]:
            evidence.append(
                f"{row.get('process_name') or 'unknown_process'} / "
                f"{row.get('thread_name') or 'unknown_thread'}: "
                f"{float(row.get('cpu_time_ms') or 0.0):.3f} ms CPU"
            )

        if not rows:
            conclusion = "No scheduler CPU rows were returned, so CPU hot spots cannot be identified from this query."
        else:
            top = rows[0]
            conclusion = (
                "CPU time is concentrated at the top returned thread: "
                f"{top.get('process_name') or 'unknown_process'} / "
                f"{top.get('thread_name') or 'unknown_thread'} with "
                f"{float(top.get('cpu_time_ms') or 0.0):.3f} ms scheduled CPU time."
            )

        recommendations = [
            "Compare top CPU threads with long slice results to separate busy CPU work from blocking waits.",
            "Filter by target process if the trace contains multiple apps or system services.",
            "Drill into sched slices near the peak window if the issue is time-localized.",
        ]
        return metrics, evidence, conclusion, recommendations

    def _select_tables_for_query(self, user_query: str, max_tables: Optional[int]) -> List[str]:
        normalized = (user_query or "").lower()
        selected = ["slice", "thread_track", "thread", "process"]
        if any(token in normalized for token in ["cpu", "sched", "调度", "占用"]):
            selected.insert(0, "sched")
        if any(token in normalized for token in ["counter", "memory", "内存", "rss", "oom"]):
            selected.extend(["counter", "counter_track"])
        if any(token in normalized for token in ["frame", "jank", "帧", "卡顿"]):
            selected.extend(["actual_frame_timeline_slice", "expected_frame_timeline_slice"])
        deduped = list(dict.fromkeys(selected))
        return deduped[: max_tables or len(deduped)]

    def _rank_tables_for_query(
        self,
        user_query: str,
        metadata: DatabaseSchemaMetadata,
        knowledge_result: KnowledgeRetrievalResult,
    ) -> List[str]:
        rule_tables = self._select_tables_for_query(user_query, max_tables=None)
        scores: Dict[str, float] = {table.name: 0.0 for table in metadata.tables}
        for index, table_name in enumerate(rule_tables):
            scores[table_name] = scores.get(table_name, 0.0) + max(20.0 - index, 1.0)
        for table_name, score in knowledge_result.table_scores.items():
            if table_name in scores:
                scores[table_name] += score * 10.0
        for table in metadata.tables:
            haystack = " ".join(
                [
                    table.name,
                    table.semantic_hint or "",
                    table.row_grain or "",
                    " ".join(column.name for column in table.columns),
                ]
            ).lower()
            for token in self._query_tokens(user_query):
                if token and token in haystack:
                    scores[table.name] += 1.0
        return [
            table.name
            for table in sorted(
                metadata.tables,
                key=lambda item: (-scores.get(item.name, 0.0), item.name),
            )
            if scores.get(table.name, 0.0) > 0.0
        ]

    def _plan_query_with_llm(
        self,
        user_query: str,
        inventory: DatabaseSchemaMetadata,
        ranked_tables: List[str],
        knowledge_hits: List[Any],
    ) -> tuple[str, str, Dict[str, Any], List[str]]:
        # Same first-stage planner shape as DatabaseTool: LLM plans intent and
        # hard/soft tables, then the system sanitizes the result.
        rule_plan, rule_notes = sanitize_query_plan_output(
            metadata=inventory,
            raw_plan=self._build_rule_query_plan(user_query, ranked_tables),
        )
        if not inventory.tables:
            return "fallback", "no_inventory_tables", rule_plan, [
                "Query planner fallback: no Perfetto inventory tables available."
            ]
        if not self.query_planner_client.is_enabled():
            return "disabled", "llm_disabled", rule_plan, [
                "Query planner skipped: LLM is disabled; used Perfetto rule plan."
            ]

        prompt_context = build_query_planner_prompt_context(
            metadata=inventory,
            knowledge_hits=knowledge_hits,
        )
        system_prompt = (
            "你是 Perfetto query planner。"
            "任务是根据用户性能问题和轻量 Perfetto 表清单，先规划分析意图与候选表。"
            "这里只能输出语义规划，不能输出 SQL。"
            "只能从给定候选表中选择 hard/soft 表。"
            "Perfetto 常见表包括 slice、thread_track、thread、process、sched、counter、counter_track、"
            "actual_frame_timeline_slice、expected_frame_timeline_slice。"
            "只输出 JSON，格式固定为："
            "{\"query_type\":\"trend|aggregate|topn|detail|distribution|lookup\","
            "\"primary_metric\":\"...\","
            "\"time_requirement\":\"required|optional|none\","
            "\"analysis_dimensions\":[\"...\"],"
            "\"filter_dimensions\":[\"...\"],"
            "\"candidate_tables_hard\":[\"table\"],"
            "\"candidate_tables_soft\":[\"table\"],"
            "\"join_needed\":true,"
            "\"reason\":\"中文说明\"}"
        )
        user_prompt = (
            f"用户问题：{user_query}\n"
            f"{prompt_context}\n"
            "请先规划分析类型、主指标、分析维度、过滤维度和候选表。"
        )

        try:
            raw_result = self.query_planner_client.chat_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.0,
                max_tokens=500,
            )
        except Exception as ex:
            return "fallback", f"planner_error:{type(ex).__name__}", rule_plan, [
                f"Query planner fallback: {type(ex).__name__}; used Perfetto rule plan."
            ]

        sanitized_plan, notes = sanitize_query_plan_output(
            metadata=inventory,
            raw_plan=raw_result,
        )
        candidate_table_count = len(sanitized_plan["candidate_tables_hard"]) + len(
            sanitized_plan["candidate_tables_soft"]
        )
        if candidate_table_count <= 0:
            notes.extend(rule_notes)
            notes.append("Query planner fallback: planner output produced no usable candidate tables.")
            return "fallback", "planner_empty", rule_plan, notes

        return "llm", sanitized_plan["reason"], sanitized_plan, notes

    def _plan_columns_with_llm(
        self,
        user_query: str,
        metadata: DatabaseSchemaMetadata,
        selected_tables: List[str],
        column_hits: List[Any],
        planning_hits: List[Any],
        query_plan_summary: str = "",
    ) -> tuple[str, str, Dict[str, List[str]], Dict[str, List[str]], List[str]]:
        # Same second-stage planner shape as DatabaseTool: LLM picks required
        # and optional columns inside the system-provided schema boundary.
        if not selected_tables:
            return "disabled", "no_selected_tables", {}, {}, [
                "Column planner skipped: no selected Perfetto tables."
            ]
        if not self.column_planner_client.is_enabled():
            return "disabled", "llm_disabled", {}, {}, [
                "Column planner skipped: LLM is disabled."
            ]

        prompt_context = build_planner_prompt_context(
            metadata=metadata,
            selected_tables=selected_tables,
            column_hits=column_hits,
            planning_hits=planning_hits,
            query_plan_summary=query_plan_summary,
        )
        system_prompt = (
            "你是 Perfetto 字段规划器。"
            "只能从给定 schema 中选择字段，不能臆造表和字段。"
            "目标是为本次 Perfetto SQL 生成找出最小必要字段集，以及少量关键辅助字段。"
            "注意 ts/dur/cpu_time 等时间字段通常是纳秒，展示毫秒需要由 SQL 除以 1e6。"
            "只输出 JSON，格式固定为："
            "{\"required_columns_by_table\":{\"table\":[\"col\"]},"
            "\"optional_columns_by_table\":{\"table\":[\"col\"]},"
            "\"reason\":\"中文简述\"}"
        )
        user_prompt = (
            f"用户问题：{user_query}\n"
            f"候选表：{selected_tables}\n"
            f"{prompt_context}\n"
            "请只从以上候选表和字段中选择。"
        )

        try:
            result = self.column_planner_client.chat_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.0,
                max_tokens=600,
            )
        except Exception as ex:
            return "fallback", f"planner_error:{type(ex).__name__}", {}, {}, [
                f"Column planner fallback: {type(ex).__name__}."
            ]

        planner_reason = str(result.get("reason", "")).strip() or "planner_success"
        required_columns, optional_columns, planner_notes = sanitize_planner_output(
            metadata=metadata,
            selected_tables=selected_tables,
            raw_required=result.get("required_columns_by_table"),
            raw_optional=result.get("optional_columns_by_table"),
        )
        planner_field_count = sum(len(item) for item in required_columns.values()) + sum(
            len(item) for item in optional_columns.values()
        )
        if planner_field_count <= 0:
            notes = list(planner_notes)
            notes.append("Column planner fallback: planner output produced no usable columns.")
            return "fallback", "planner_empty", {}, {}, notes

        return "llm", planner_reason, required_columns, optional_columns, planner_notes

    def _build_rule_query_plan(self, user_query: str, ranked_tables: List[str]) -> Dict[str, Any]:
        normalized = (user_query or "").lower()
        hard = ["slice", "thread_track", "thread", "process"]
        soft: List[str] = []
        primary_metric = "duration_ms"
        analysis_dimensions = ["process", "thread", "slice"]
        filter_dimensions = ["duration_threshold"]
        query_type = "topn"
        join_needed = True
        reason = "perfetto_long_slice_rule"

        if any(token in normalized for token in ["cpu", "sched", "调度", "占用"]):
            hard = ["sched", "thread", "process"]
            soft = ["slice", "thread_track"]
            primary_metric = "cpu_time_ms"
            analysis_dimensions = ["process", "thread", "cpu"]
            filter_dimensions = []
            reason = "perfetto_cpu_rule"
        elif any(token in normalized for token in ["counter", "memory", "内存", "rss", "oom"]):
            hard = ["counter", "counter_track"]
            soft = ["process", "thread"]
            primary_metric = "counter_value"
            analysis_dimensions = ["counter", "process"]
            filter_dimensions = ["counter_name", "time"]
            query_type = "trend"
            reason = "perfetto_counter_rule"
        elif any(token in normalized for token in ["frame", "jank", "帧", "卡顿"]):
            hard = ["slice", "thread_track", "thread", "process"]
            soft = ["actual_frame_timeline_slice", "expected_frame_timeline_slice"]
            primary_metric = "frame_or_slice_duration_ms"
            analysis_dimensions = ["frame", "thread", "process"]
            filter_dimensions = ["duration_threshold"]
            reason = "perfetto_frame_or_slice_rule"

        ranked_set = set(ranked_tables)
        if ranked_set:
            soft.extend(table_name for table_name in ranked_tables if table_name not in set(hard))

        return {
            "query_type": query_type,
            "primary_metric": primary_metric,
            "time_requirement": "optional",
            "analysis_dimensions": analysis_dimensions,
            "filter_dimensions": filter_dimensions,
            "candidate_tables_hard": hard,
            "candidate_tables_soft": soft,
            "join_needed": join_needed,
            "reason": reason,
        }

    def _build_column_priorities(
        self,
        metadata: DatabaseSchemaMetadata,
        selected_tables: List[str],
        column_hits: List[Any],
        query_plan: Dict[str, Any],
    ) -> Dict[str, List[str]]:
        selected = set(selected_tables)
        priorities: Dict[str, List[str]] = {}
        metric = str(query_plan.get("primary_metric", "") or "").lower()

        def add(table_name: str, column_names: Iterable[str]) -> None:
            if table_name not in selected:
                return
            priorities.setdefault(table_name, [])
            seen = set(priorities[table_name])
            for column_name in column_names:
                if column_name and column_name not in seen:
                    priorities[table_name].append(column_name)
                    seen.add(column_name)

        for relation in metadata.relationships:
            if relation.from_table in selected and relation.to_table in selected:
                add(relation.from_table, relation.from_columns)
                add(relation.to_table, relation.to_columns)

        for table in metadata.tables:
            if table.name not in selected:
                continue
            time_columns = [
                column.name
                for column in table.columns
                if any(marker in column.name.lower() for marker in ["ts", "time", "dur"])
            ]
            add(table.name, table.primary_key)
            add(table.name, time_columns)
            add(table.name, ["name"])

        if "cpu" in metric:
            add("sched", ["dur", "cpu", "utid", "end_state"])
            add("thread", ["utid", "upid", "name"])
            add("process", ["upid", "name"])
        elif "counter" in metric:
            add("counter", ["ts", "track_id", "value"])
            add("counter_track", ["id", "name", "upid", "utid"])
        elif "frame" in metric:
            add("actual_frame_timeline_slice", ["ts", "dur", "name", "jank_type"])
            add("expected_frame_timeline_slice", ["ts", "dur", "name"])
        else:
            add("slice", ["ts", "dur", "track_id", "name", "category"])
            add("thread_track", ["id", "utid", "name"])
            add("thread", ["utid", "upid", "name"])
            add("process", ["upid", "name"])

        for hit in column_hits:
            table_name = getattr(hit, "table_name", "")
            column_name = getattr(hit, "column_name", "")
            add(str(table_name), [str(column_name)])

        return priorities

    def _slice_schema_metadata(
        self,
        metadata: DatabaseSchemaMetadata,
        selected_tables: List[str],
        max_columns_per_table: int,
        prioritized_columns_by_table: Dict[str, List[str]],
    ) -> DatabaseSchemaMetadata:
        selected = set(selected_tables)
        sliced_tables: List[TableSchema] = []
        for table in metadata.tables:
            if table.name not in selected:
                continue
            priority = prioritized_columns_by_table.get(table.name, [])
            columns_by_name = {column.name: column for column in table.columns}
            ordered_columns: List[SchemaColumn] = []
            seen = set()
            for column_name in priority:
                column = columns_by_name.get(column_name)
                if column is None or column.name in seen:
                    continue
                ordered_columns.append(column)
                seen.add(column.name)
            for column in table.columns:
                if len(ordered_columns) >= max_columns_per_table:
                    break
                if column.name in seen:
                    continue
                ordered_columns.append(column)
                seen.add(column.name)
            sliced_tables.append(
                table.model_copy(update={"columns": ordered_columns})
                if hasattr(table, "model_copy")
                else table.copy(update={"columns": ordered_columns})
            )

        relationships = [
            relation
            for relation in metadata.relationships
            if relation.from_table in selected and relation.to_table in selected
        ]
        return DatabaseSchemaMetadata(
            source="perfetto",
            schema_name=metadata.schema_name,
            tables=sliced_tables,
            relationships=relationships,
            notes=list(metadata.notes),
        )

    @staticmethod
    def _query_tokens(user_query: str) -> List[str]:
        return [
            token
            for token in re.split(r"[^A-Za-z0-9_\u4e00-\u9fff]+", (user_query or "").lower())
            if token
        ]

    def _infer_primary_metric(self, user_query: str) -> str:
        normalized = (user_query or "").lower()
        if any(token in normalized for token in ["cpu", "sched", "调度", "占用"]):
            return "cpu_time_ms"
        if any(token in normalized for token in ["frame", "jank", "帧", "卡顿"]):
            return "frame_duration_ms"
        return "duration_ms"

    def _builtin_perfetto_tables(self) -> List[TableSchema]:
        return [
            self._table(
                "slice",
                "Trace slice intervals, commonly used for task, method, frame, and async event duration analysis",
                "one timed slice",
                [
                    ("id", "integer", "Slice id"),
                    ("ts", "integer", "Start timestamp in nanoseconds"),
                    ("dur", "integer", "Duration in nanoseconds; divide by 1e6 for ms"),
                    ("track_id", "integer", "Track that owns the slice"),
                    ("name", "string", "Slice name"),
                    ("category", "string", "Optional event category"),
                    ("depth", "integer", "Nested slice depth"),
                ],
            ),
            self._table(
                "thread_track",
                "Maps thread-owned tracks to thread ids",
                "one thread track",
                [
                    ("id", "integer", "Track id, join with slice.track_id"),
                    ("utid", "integer", "Unique thread id, join with thread.utid"),
                    ("name", "string", "Track name"),
                ],
            ),
            self._table(
                "thread",
                "Thread metadata",
                "one thread",
                [
                    ("utid", "integer", "Unique thread id"),
                    ("tid", "integer", "OS thread id"),
                    ("upid", "integer", "Unique process id, join with process.upid"),
                    ("name", "string", "Thread name"),
                ],
            ),
            self._table(
                "process",
                "Process metadata",
                "one process",
                [
                    ("upid", "integer", "Unique process id"),
                    ("pid", "integer", "OS process id"),
                    ("name", "string", "Process name"),
                ],
            ),
            self._table(
                "sched",
                "CPU scheduler slices; useful for CPU time and scheduling analysis",
                "one scheduled thread interval",
                [
                    ("ts", "integer", "Start timestamp in nanoseconds"),
                    ("dur", "integer", "Scheduled duration in nanoseconds"),
                    ("cpu", "integer", "CPU core"),
                    ("utid", "integer", "Unique thread id, join with thread.utid"),
                    ("end_state", "string", "Scheduler end state"),
                ],
            ),
            self._table(
                "counter",
                "Counter samples over time",
                "one counter sample",
                [
                    ("ts", "integer", "Timestamp in nanoseconds"),
                    ("track_id", "integer", "Counter track id"),
                    ("value", "double", "Counter value"),
                ],
            ),
            self._table(
                "counter_track",
                "Counter track metadata",
                "one counter track",
                [
                    ("id", "integer", "Counter track id"),
                    ("name", "string", "Counter name"),
                    ("upid", "integer", "Optional owning process id"),
                    ("utid", "integer", "Optional owning thread id"),
                ],
            ),
            self._table(
                "actual_frame_timeline_slice",
                "Android actual frame timeline slices when frame timeline data exists",
                "one actual frame",
                [
                    ("id", "integer", "Frame slice id"),
                    ("ts", "integer", "Frame start timestamp in nanoseconds"),
                    ("dur", "integer", "Frame duration in nanoseconds"),
                    ("name", "string", "Frame name"),
                    ("jank_type", "string", "Jank classification when available"),
                ],
            ),
            self._table(
                "expected_frame_timeline_slice",
                "Android expected frame timeline slices when frame timeline data exists",
                "one expected frame",
                [
                    ("id", "integer", "Frame slice id"),
                    ("ts", "integer", "Expected frame start timestamp in nanoseconds"),
                    ("dur", "integer", "Expected frame duration in nanoseconds"),
                    ("name", "string", "Frame name"),
                ],
            ),
        ]

    def _table(
        self,
        name: str,
        hint: str,
        grain: str,
        columns: Iterable[tuple[str, str, str]],
    ) -> TableSchema:
        return TableSchema(
            name=name,
            semantic_hint=hint,
            row_grain=grain,
            columns=[
                SchemaColumn(name=column_name, data_type=data_type, semantic_hint=semantic_hint)
                for column_name, data_type, semantic_hint in columns
            ],
        )

    def _builtin_perfetto_relationships(self) -> List[SchemaRelationship]:
        return [
            SchemaRelationship(
                name="slice__thread_track",
                from_table="slice",
                from_columns=["track_id"],
                to_table="thread_track",
                to_columns=["id"],
                description="Map slices to thread tracks.",
            ),
            SchemaRelationship(
                name="thread_track__thread",
                from_table="thread_track",
                from_columns=["utid"],
                to_table="thread",
                to_columns=["utid"],
                description="Map thread tracks to thread metadata.",
            ),
            SchemaRelationship(
                name="thread__process",
                from_table="thread",
                from_columns=["upid"],
                to_table="process",
                to_columns=["upid"],
                description="Map threads to owning process metadata.",
            ),
            SchemaRelationship(
                name="sched__thread",
                from_table="sched",
                from_columns=["utid"],
                to_table="thread",
                to_columns=["utid"],
                description="Map scheduler rows to thread metadata.",
            ),
            SchemaRelationship(
                name="counter__counter_track",
                from_table="counter",
                from_columns=["track_id"],
                to_table="counter_track",
                to_columns=["id"],
                description="Map counter samples to counter metadata.",
            ),
            SchemaRelationship(
                name="counter_track__process",
                from_table="counter_track",
                from_columns=["upid"],
                to_table="process",
                to_columns=["upid"],
                description="Map process-scoped counters to process metadata.",
            ),
        ]
