from __future__ import annotations

import os
import re
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, Iterable, List, Optional

from DataAnalyze.schemas.models import (
    DatabaseSchemaMetadata,
    PerfettoAnalyzeResponse,
    PerfettoMetric,
    SQLExecutionResult,
    SchemaColumn,
    SchemaSelectionResult,
    TableSchema,
)
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
        self._processor: Optional[Any] = None

    def get_schema(self) -> str:
        return self.render_schema_prompt(self.get_schema_metadata())

    def get_schema_metadata(self) -> DatabaseSchemaMetadata:
        return DatabaseSchemaMetadata(
            source="perfetto",
            schema_name="perfetto",
            tables=self._builtin_perfetto_tables(),
            relationships=[],
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
        metadata = self.get_schema_metadata()
        selected_tables = self._select_tables_for_query(user_query, max_tables=max_tables)
        selected_schema = DatabaseSchemaMetadata(
            source="perfetto",
            schema_name="perfetto",
            tables=[table for table in metadata.tables if table.name in selected_tables],
            relationships=[],
            notes=list(metadata.notes),
        )
        prompt_text = self.render_schema_prompt(
            selected_schema,
            max_tables=max_tables,
            max_columns_per_table=max_columns_per_table,
        )
        return SchemaSelectionResult(
            strategy="perfetto-focused",
            metadata_source="perfetto",
            selected_schema=selected_schema,
            selected_tables=[table.name for table in selected_schema.tables],
            selected_relationships=[],
            prompt_text=prompt_text,
            prompt_budget_chars=len(prompt_text),
            fetch_mode="builtin-perfetto",
            discovery_tables=selected_tables,
            query_planner_strategy="rule",
            query_planner_reason="Perfetto MVP uses curated trace tables and metric patterns.",
            query_planner_query_type="performance_trace",
            query_planner_primary_metric=self._infer_primary_metric(user_query),
            query_planner_time_requirement="optional",
            query_planner_analysis_dimensions=["process", "thread", "slice"],
            query_planner_filter_dimensions=[],
            query_planner_candidate_tables_hard=selected_tables[: max_tables or len(selected_tables)],
            query_planner_candidate_tables_soft=[],
            query_planner_join_needed=True,
            column_selection_strategy="curated-perfetto",
            selected_columns_by_table={
                table.name: [column.name for column in table.columns]
                for table in selected_schema.tables
            },
            column_planner_strategy="rule",
            column_planner_reason="Curated Perfetto columns are small enough for the first pass.",
            knowledge_strategy="perfetto-builtin",
            retrieval_notes=list(selected_schema.notes),
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
