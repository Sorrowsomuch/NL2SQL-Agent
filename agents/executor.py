from __future__ import annotations

import json
import re
from time import perf_counter
from typing import Any, Dict, List, Optional

from DataAnalyze.agents.base import BaseAgent
from DataAnalyze.config import EXECUTOR_LLM_CONFIG
from DataAnalyze.core.memory import MemoryManager
from DataAnalyze.middleware.metrics import (
    observe_db_query,
    observe_executor_analysis_strategy,
    observe_executor_result,
    observe_executor_sql_generation,
    observe_executor_sql_guard,
)
from DataAnalyze.middleware.monitor import BaseMonitor
from DataAnalyze.schemas.models import (
    AgentResponse,
    ChartConfig,
    ChartSeries,
    SchemaSelectionResult,
    ToolCallTrace,
)
from DataAnalyze.tools.db.db_tool import DatabaseTool
from DataAnalyze.tools.llm_tool import LLMClient, LLMEndpointConfig


class ExecutorAgent(BaseAgent):
    """Executor：负责选 schema、生成 SQL、执行查询并输出分析结果。"""

    _AGGREGATE_FUNCTIONS = {
        "avg",
        "count",
        "json_agg",
        "jsonb_agg",
        "max",
        "min",
        "string_agg",
        "sum",
    }

    def __init__(
        self,
        db_tool: DatabaseTool,
        memory_manager: MemoryManager,
        monitor: Optional[BaseMonitor] = None,
    ) -> None:
        super().__init__(name="executor")
        self.db_tool = db_tool
        self.memory_manager = memory_manager
        self.llm_client = LLMClient(
            LLMEndpointConfig(
                base_url=EXECUTOR_LLM_CONFIG.base_url,
                api_key=EXECUTOR_LLM_CONFIG.api_key,
                model=EXECUTOR_LLM_CONFIG.model,
                timeout_sec=EXECUTOR_LLM_CONFIG.timeout_sec,
            )
        )
        self._runner = self._run_impl
        if monitor is not None:
            self._runner = monitor.monitor("ExecutorAgent.run")(self._runner)

    def run(
        self,
        session_id: str,
        user_query: str,
        retry_count: int = 0,
        last_error: Optional[str] = None,
    ) -> AgentResponse:
        return self._runner(
            session_id=session_id,
            user_query=user_query,
            retry_count=retry_count,
            last_error=last_error,
        )

    def _run_impl(
        self,
        session_id: str,
        user_query: str,
        retry_count: int = 0,
        last_error: Optional[str] = None,
    ) -> AgentResponse:
        traces: List[ToolCallTrace] = []

        context_records = self.memory_manager.build_prompt_context(
            session_id=session_id,
            user_input=user_query,
            l0_window=10,
            fact_top_k=8,
            summary_keep_count=4,
        )

        schema_start = perf_counter()
        schema_context = self.db_tool.select_schema_context(
            user_query=user_query,
            retry_count=retry_count,
        )
        traces.append(
            ToolCallTrace(
                tool_name="select_schema_context",
                arguments={
                    "strategy": schema_context.strategy,
                    "metadata_source": schema_context.metadata_source,
                    "fetch_mode": schema_context.fetch_mode,
                    "discovery_tables": schema_context.discovery_tables,
                    "query_planner_strategy": schema_context.query_planner_strategy,
                    "query_planner_reason": schema_context.query_planner_reason,
                    "query_planner_query_type": schema_context.query_planner_query_type,
                    "query_planner_primary_metric": schema_context.query_planner_primary_metric,
                    "query_planner_time_requirement": schema_context.query_planner_time_requirement,
                    "query_planner_analysis_dimensions": schema_context.query_planner_analysis_dimensions,
                    "query_planner_filter_dimensions": schema_context.query_planner_filter_dimensions,
                    "query_planner_candidate_tables_hard": schema_context.query_planner_candidate_tables_hard,
                    "query_planner_candidate_tables_soft": schema_context.query_planner_candidate_tables_soft,
                    "query_planner_join_needed": schema_context.query_planner_join_needed,
                    "column_selection_strategy": schema_context.column_selection_strategy,
                    "column_planner_strategy": schema_context.column_planner_strategy,
                    "column_planner_reason": schema_context.column_planner_reason,
                    "planner_required_columns_by_table": schema_context.planner_required_columns_by_table,
                    "planner_optional_columns_by_table": schema_context.planner_optional_columns_by_table,
                    "selected_columns_by_table": schema_context.selected_columns_by_table,
                    "selected_tables": schema_context.selected_tables,
                    "selected_relationships": schema_context.selected_relationships,
                    "prompt_budget_chars": schema_context.prompt_budget_chars,
                    "knowledge_strategy": schema_context.knowledge_strategy,
                    "knowledge_hit_ids": schema_context.knowledge_hit_ids,
                    "knowledge_hit_titles": schema_context.knowledge_hit_titles,
                    "knowledge_column_hints": schema_context.knowledge_column_hints,
                },
                success=True,
                output_preview=schema_context.prompt_text,
                duration_ms=(perf_counter() - schema_start) * 1000.0,
            )
        )

        sql, sql_strategy, sql_guard_debug = self._generate_sql(
            user_query=user_query,
            schema_context=schema_context,
            retry_count=retry_count,
            last_error=last_error,
            context_records=context_records,
        )
        observe_executor_sql_generation(sql_strategy)
        observe_executor_sql_guard(
            outcome=str(sql_guard_debug.get("outcome", "unknown")),
            reason=str(sql_guard_debug.get("reason", "unknown")),
        )
        traces.append(
            ToolCallTrace(
                tool_name="validate_generated_sql",
                arguments=sql_guard_debug,
                success=bool(sql_guard_debug.get("success", True)),
                output_preview=(
                    f"strategy={sql_strategy}; outcome={sql_guard_debug.get('outcome')}; "
                    f"reason={sql_guard_debug.get('reason')}"
                ),
                duration_ms=0.0,
            )
        )
        traces.append(
            ToolCallTrace(
                tool_name="execute_sql",
                arguments={"sql": sql},
                success=True,
                output_preview="SQL prepared",
                duration_ms=0.0,
            )
        )

        try:
            query_result = self.db_tool.execute_sql(sql)
            observe_db_query(
                source=str(query_result.get("source", "unknown")),
                success=True,
                duration_ms=float(query_result.get("duration_ms", 0.0)),
            )
            traces[-1] = ToolCallTrace(
                tool_name="execute_sql",
                arguments={"sql": sql},
                success=True,
                output_preview=(
                    f"rows={query_result.get('row_count', 0)} "
                    f"source={query_result.get('source', 'postgres')}"
                ),
                duration_ms=float(query_result.get("duration_ms", 0.0)),
            )
        except Exception as ex:
            observe_db_query(source="unknown", success=False, duration_ms=0.0)
            observe_executor_result(success=False, rows=0, chart_type="none")
            traces[-1] = ToolCallTrace(
                tool_name="execute_sql",
                arguments={"sql": sql},
                success=False,
                output_preview=str(ex),
                duration_ms=0.0,
            )
            return AgentResponse(
                success=False,
                text_reply="执行 SQL 时发生错误，已交给 Reviewer 判断是否重试。",
                sql=sql,
                columns=[],
                rows=[],
                tool_calls=traces,
                error_reason=str(ex),
                state="EXECUTOR_FAILED",
                retry_count=retry_count,
            )

        columns = query_result.get("columns", [])
        rows = query_result.get("rows", [])
        row_count = int(query_result.get("row_count", 0))
        analysis = self._analyze_result_with_llm(
            user_query=user_query,
            sql=sql,
            rows=rows,
            fallback_context_size=len(context_records),
        )
        analysis_strategy = str(analysis.get("_analysis_strategy", "fallback"))
        observe_executor_analysis_strategy(analysis_strategy)
        chart = analysis.get("chart") or self._build_chart(rows)
        chart_type = chart.chart_type if chart is not None else "none"
        observe_executor_result(success=True, rows=row_count, chart_type=chart_type)
        text_reply = str(
            analysis.get("text_reply")
            or self._build_text_reply(
                user_query=user_query,
                row_count=row_count,
                rows=rows,
                context_size=len(context_records),
            )
        )
        findings = analysis.get("professional_findings") or []
        recommendations = analysis.get("recommendations") or []

        return AgentResponse(
            success=True,
            text_reply=text_reply,
            chart=chart,
            professional_findings=[str(item) for item in findings][:8],
            recommendations=[str(item) for item in recommendations][:8],
            sql=sql,
            columns=columns,
            rows=rows,
            tool_calls=traces,
            error_reason=None,
            state="EXECUTOR_DONE",
            retry_count=retry_count,
        )

    def _generate_sql(
        self,
        user_query: str,
        schema_context: SchemaSelectionResult,
        retry_count: int,
        last_error: Optional[str],
        context_records: List[Any],
    ) -> tuple[str, str, Dict[str, Any]]:
        llm_sql = self._generate_sql_with_llm(
            user_query=user_query,
            schema_context=schema_context,
            retry_count=retry_count,
            last_error=last_error,
            context_records=context_records,
        )
        if llm_sql is not None:
            sql, outcome, reason = llm_sql
            strategy = "llm_repaired" if outcome == "repaired" else "llm"
            return sql, strategy, {
                "success": True,
                "outcome": outcome,
                "reason": reason,
                "final_sql_source": strategy,
            }

        fallback_sql = self._build_fallback_sql(
            user_query=user_query,
            schema_context=schema_context,
            retry_count=retry_count,
            last_error=last_error,
        )
        fallback_valid, fallback_reason = self._validate_generated_sql(fallback_sql)
        return fallback_sql, "fallback", {
            "success": fallback_valid,
            "outcome": "fallback",
            "reason": fallback_reason,
            "final_sql_source": "fallback",
        }

    def _generate_sql_with_llm(
        self,
        user_query: str,
        schema_context: SchemaSelectionResult,
        retry_count: int,
        last_error: Optional[str],
        context_records: List[Any],
    ) -> Optional[tuple[str, str, str]]:
        if not self.llm_client.is_enabled():
            return None

        context_preview = [getattr(item, "content", "") for item in context_records[-8:]]
        system_prompt = (
            "你是资深数据分析 SQL 生成器。"
            "只输出 JSON，格式为 {\"sql\":\"SELECT ...\",\"reason\":\"...\"}。"
            "必须生成单条只读 SQL，必须以 SELECT 开头，不允许注释和多语句。"
            "优先使用给定的候选表与关系，不要臆造字段。"
            "如果同时需要窗口函数和聚合函数，必须拆成外层 SELECT + 内层子查询。"
            "严禁把 window function 直接写进 aggregate function 的参数中。"
        )
        user_prompt = (
            f"用户问题: {user_query}\n"
            f"Schema 策略: {schema_context.strategy}\n"
            f"候选表: {schema_context.selected_tables}\n"
            f"候选关系: {schema_context.selected_relationships}\n"
            f"可用 Schema:\n{schema_context.prompt_text}\n"
            f"重试次数: {retry_count}\n"
            f"上次错误: {last_error or '无'}\n"
            f"上下文片段: {json.dumps(context_preview, ensure_ascii=False)}"
        )
        if self._should_strengthen_window_guard(last_error):
            user_prompt += (
                "\n额外要求：上次 SQL 因聚合函数参数中嵌套窗口函数而失败。"
                "这次必须先在子查询中算窗口列，再在外层做聚合。"
            )

        try:
            result = self.llm_client.chat_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        except Exception:
            return None

        sql = str(result.get("sql", "")).strip()
        is_valid, reason = self._validate_generated_sql(sql)
        if is_valid:
            return sql, "passed", reason

        repaired_sql = self._repair_sql_with_llm(
            user_query=user_query,
            original_sql=sql,
            validation_reason=reason,
            schema_context=schema_context,
            retry_count=retry_count,
            last_error=last_error,
        )
        if repaired_sql is None:
            return None
        return repaired_sql, "repaired", reason

    def _repair_sql_with_llm(
        self,
        user_query: str,
        original_sql: str,
        validation_reason: str,
        schema_context: SchemaSelectionResult,
        retry_count: int,
        last_error: Optional[str],
    ) -> Optional[str]:
        if not self.llm_client.is_enabled():
            return None

        system_prompt = (
            "你是 PostgreSQL SQL 修复器。"
            "只输出 JSON，格式为 {\"sql\":\"SELECT ...\",\"reason\":\"...\"}。"
            "必须保留原始查询意图，但要修复 PostgreSQL 不接受的写法。"
            "如果问题涉及聚合函数与窗口函数冲突，必须改成外层 SELECT + 内层子查询的两段式写法。"
            "不允许输出非 SELECT 语句。"
        )
        user_prompt = (
            f"用户问题: {user_query}\n"
            f"原始 SQL:\n{original_sql}\n"
            f"校验失败原因: {validation_reason}\n"
            f"上次执行错误: {last_error or '无'}\n"
            f"候选表: {schema_context.selected_tables}\n"
            f"可用 Schema:\n{schema_context.prompt_text}\n"
            "请返回一条修复后的 PostgreSQL SELECT SQL。"
        )
        try:
            result = self.llm_client.chat_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        except Exception:
            return None

        repaired_sql = str(result.get("sql", "")).strip()
        is_valid, _ = self._validate_generated_sql(repaired_sql)
        if not is_valid:
            return None
        return repaired_sql

    def _validate_generated_sql(self, sql: str) -> tuple[bool, str]:
        normalized = (sql or "").strip()
        if not normalized:
            return False, "empty_sql"
        if not normalized.upper().startswith("SELECT"):
            return False, "not_select"
        if ";" in normalized.rstrip(";"):
            return False, "multi_statement"
        if "--" in normalized or "/*" in normalized:
            return False, "comment_not_allowed"
        if self._has_aggregate_window_conflict(normalized):
            return False, "aggregate_window_conflict"
        return True, "ok"

    def _has_aggregate_window_conflict(self, sql: str) -> bool:
        # 轻量检查：识别“聚合函数参数内部出现 OVER”这类 PostgreSQL 常见报错模式。
        tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[(),]", sql)
        paren_stack: List[Optional[bool]] = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            lowered = token.lower()
            next_token = tokens[index + 1] if index + 1 < len(tokens) else None

            if (
                re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", token)
                and next_token == "("
                and lowered != "over"
            ):
                paren_stack.append(lowered in self._AGGREGATE_FUNCTIONS)
                index += 2
                continue

            if token == "(":
                paren_stack.append(None)
            elif token == ")":
                if paren_stack:
                    paren_stack.pop()
            elif lowered == "over":
                if any(item is True for item in paren_stack):
                    return True
            index += 1
        return False

    def _should_strengthen_window_guard(self, last_error: Optional[str]) -> bool:
        if not last_error:
            return False
        normalized = last_error.lower()
        return (
            "window function" in normalized
            and "aggregate function" in normalized
        ) or "aggregate function calls cannot contain window function calls" in normalized

    def _build_fallback_sql(
        self,
        user_query: str,
        schema_context: SchemaSelectionResult,
        retry_count: int,
        last_error: Optional[str],
    ) -> str:
        normalized = user_query.strip().lower()
        selected_tables = set(schema_context.selected_tables)

        if retry_count > 0 and last_error:
            if {"chat_memories", "chat_sessions"}.issubset(selected_tables):
                return (
                    "SELECT m.session_id, s.title, m.memory_layer, m.memory_type, m.created_at "
                    "FROM chat_memories AS m "
                    "LEFT JOIN chat_sessions AS s ON s.session_id = m.session_id "
                    "ORDER BY m.created_at DESC LIMIT 20"
                )
            return (
                "SELECT ts, service, level, error_code, message "
                "FROM ops_log_event ORDER BY ts DESC LIMIT 20"
            )

        if "schema" in normalized or "表结构" in normalized:
            return (
                "SELECT table_name, column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' "
                "ORDER BY table_name, ordinal_position LIMIT 200"
            )

        if any(token in normalized for token in ["会话", "session", "记忆", "memory"]):
            if {"chat_memories", "chat_sessions"}.issubset(selected_tables):
                return (
                    "SELECT m.session_id, s.title, m.role, m.memory_layer, m.memory_type, "
                    "m.created_at, m.salience_score "
                    "FROM chat_memories AS m "
                    "LEFT JOIN chat_sessions AS s ON s.session_id = m.session_id "
                    "ORDER BY m.created_at DESC LIMIT 20"
                )
            return (
                "SELECT session_id, role, memory_layer, memory_type, created_at "
                "FROM chat_memories ORDER BY created_at DESC LIMIT 20"
            )

        if "数量" in normalized or "count" in normalized:
            return (
                "SELECT "
                "SUM(CASE WHEN level = 'ERROR' THEN 1 ELSE 0 END) AS error_count, "
                "SUM(CASE WHEN level = 'WARN' THEN 1 ELSE 0 END) AS warn_count, "
                "SUM(CASE WHEN level = 'INFO' THEN 1 ELSE 0 END) AS info_count "
                "FROM ops_log_event"
            )

        if any(token in normalized for token in ["延迟", "latency", "p95"]):
            return (
                "SELECT service, AVG(latency_ms) AS avg_latency, "
                "MAX(latency_ms) AS p95_latency "
                "FROM ops_log_event GROUP BY service ORDER BY avg_latency DESC LIMIT 10"
            )

        if any(token in normalized for token in ["错误", "error"]):
            return (
                "SELECT ts, service, level, error_code, message "
                "FROM ops_log_event WHERE level = 'ERROR' "
                "ORDER BY ts DESC LIMIT 20"
            )

        if "ops_log_event" in selected_tables:
            return (
                "SELECT ts, service, host, level, message "
                "FROM ops_log_event ORDER BY ts DESC LIMIT 20"
            )

        if "chat_memories" in selected_tables:
            return (
                "SELECT session_id, role, memory_layer, memory_type, created_at "
                "FROM chat_memories ORDER BY created_at DESC LIMIT 20"
            )

        if "chat_sessions" in selected_tables:
            return (
                "SELECT session_id, title, created_at, updated_at "
                "FROM chat_sessions ORDER BY updated_at DESC LIMIT 20"
            )

        return "SELECT ts, service, host, level, message FROM ops_log_event ORDER BY ts DESC LIMIT 20"

    def _analyze_result_with_llm(
        self,
        user_query: str,
        sql: str,
        rows: List[Dict[str, Any]],
        fallback_context_size: int,
    ) -> Dict[str, Any]:
        if not self.llm_client.is_enabled():
            return {
                "_analysis_strategy": "fallback",
                "text_reply": self._build_text_reply(
                    user_query=user_query,
                    row_count=len(rows),
                    rows=rows,
                    context_size=fallback_context_size,
                ),
                "professional_findings": self._fallback_findings(rows),
                "recommendations": self._fallback_recommendations(rows),
                "chart": self._build_chart(rows),
            }

        system_prompt = (
            "你是企业级日志与数据库分析专家。"
            "根据 SQL 查询结果生成专业分析。"
            "仅输出 JSON，格式为 "
            "{\"text_reply\":\"...\",\"professional_findings\":[\"...\"],"
            "\"recommendations\":[\"...\"],"
            "\"chart\":{\"chart_type\":\"line|bar|pie|table\",\"title\":\"...\","
            "\"x_axis\":[...],\"series\":[{\"name\":\"...\",\"data\":[...]}]}}"
        )
        preview_rows = rows[:80]
        user_prompt = (
            f"问题: {user_query}\n"
            f"SQL: {sql}\n"
            f"行数: {len(rows)}\n"
            f"结果样本(JSON): {json.dumps(preview_rows, ensure_ascii=False, default=str)}"
        )

        try:
            result = self.llm_client.chat_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            parsed_chart = self._safe_chart(result.get("chart"), rows)
            return {
                "_analysis_strategy": "llm",
                "text_reply": str(result.get("text_reply", "")).strip()
                or self._build_text_reply(
                    user_query=user_query,
                    row_count=len(rows),
                    rows=rows,
                    context_size=fallback_context_size,
                ),
                "professional_findings": result.get("professional_findings")
                or self._fallback_findings(rows),
                "recommendations": result.get("recommendations")
                or self._fallback_recommendations(rows),
                "chart": parsed_chart,
            }
        except Exception:
            return {
                "_analysis_strategy": "fallback",
                "text_reply": self._build_text_reply(
                    user_query=user_query,
                    row_count=len(rows),
                    rows=rows,
                    context_size=fallback_context_size,
                ),
                "professional_findings": self._fallback_findings(rows),
                "recommendations": self._fallback_recommendations(rows),
                "chart": self._build_chart(rows),
            }

    def _safe_chart(self, raw_chart: Any, rows: List[Dict[str, Any]]) -> Optional[ChartConfig]:
        if not isinstance(raw_chart, dict):
            return self._build_chart(rows)
        try:
            chart_type = str(raw_chart.get("chart_type", "table")).lower()
            if chart_type not in {"line", "bar", "pie", "table"}:
                chart_type = "table"
            series_raw = raw_chart.get("series") or []
            series: List[ChartSeries] = []
            for item in series_raw:
                if not isinstance(item, dict):
                    continue
                data = item.get("data") or []
                numeric = [float(x) for x in data if isinstance(x, (int, float))]
                series.append(ChartSeries(name=str(item.get("name", "series")), data=numeric))

            if chart_type == "table" or not series:
                inferred = self._infer_chart_from_rows(rows)
                if inferred is not None:
                    return inferred

            return ChartConfig(
                chart_type=chart_type,
                title=str(raw_chart.get("title", "分析结果")),
                x_axis=[str(x) for x in (raw_chart.get("x_axis") or [])],
                series=series,
            )
        except Exception:
            return self._build_chart(rows)

    def _fallback_findings(self, rows: List[Dict[str, Any]]) -> List[str]:
        if not rows:
            return ["未查询到数据，建议检查时间窗口或过滤条件。"]

        findings = [f"本次共返回 {len(rows)} 行数据。"]
        first = rows[0]
        if "service" in first:
            services = sorted({str(row.get("service", "unknown")) for row in rows})
            findings.append(f"涉及服务: {', '.join(services[:6])}")
        if "level" in first:
            findings.append("结果中包含日志级别字段，可继续做错误率与趋势分析。")
        if "session_id" in first:
            findings.append("结果涉及会话维度，可继续按会话追踪记忆演化。")
        return findings[:6]

    def _fallback_recommendations(self, rows: List[Dict[str, Any]]) -> List[str]:
        if not rows:
            return [
                "扩大时间窗口到最近 24 小时后重试。",
                "适当放宽 service、level 或 session 过滤条件。",
            ]
        return [
            "建议补充趋势型 SQL，确认指标是否存在明显峰值。",
            "建议增加维度拆分，例如 service、error_code 或 memory_layer。",
            "如果需要根因分析，可进一步按 trace_id 或 session_id 下钻。",
        ]

    def _build_chart(self, rows: List[Dict[str, Any]]) -> Optional[ChartConfig]:
        if not rows:
            return None

        inferred = self._infer_chart_from_rows(rows)
        if inferred is not None:
            return inferred

        first = rows[0]
        if {"error_count", "warn_count", "info_count"}.issubset(first.keys()):
            return ChartConfig(
                chart_type="bar",
                title="日志级别分布",
                x_axis=["ERROR", "WARN", "INFO"],
                series=[
                    ChartSeries(
                        name="数量",
                        data=[
                            float(first.get("error_count", 0)),
                            float(first.get("warn_count", 0)),
                            float(first.get("info_count", 0)),
                        ],
                    )
                ],
            )

        if "service" in first and "avg_latency" in first:
            x_axis = [str(item.get("service", "unknown")) for item in rows]
            data = [float(item.get("avg_latency", 0.0)) for item in rows]
            return ChartConfig(
                chart_type="line",
                title="服务平均延迟",
                x_axis=x_axis,
                series=[ChartSeries(name="avg_latency", data=data)],
            )

        if "service" in first and "latency_ms" in first:
            x_axis = [str(item.get("service", "unknown")) for item in rows[:10]]
            data = [float(item.get("latency_ms", 0.0)) for item in rows[:10]]
            return ChartConfig(
                chart_type="bar",
                title="日志延迟采样",
                x_axis=x_axis,
                series=[ChartSeries(name="latency_ms", data=data)],
            )

        return ChartConfig(chart_type="table", title="查询明细", x_axis=[], series=[])

    def _infer_chart_from_rows(self, rows: List[Dict[str, Any]]) -> Optional[ChartConfig]:
        if not rows:
            return None

        sample = rows[0]
        keys = list(sample.keys())
        numeric_cols: List[str] = []
        for key in keys:
            values = [row.get(key) for row in rows[:40]]
            non_null = [value for value in values if value is not None]
            if non_null and all(isinstance(value, (int, float)) for value in non_null):
                numeric_cols.append(key)

        time_col = None
        for key in keys:
            lowered = str(key).lower()
            if any(marker in lowered for marker in ["ts", "time", "date", "created"]):
                time_col = key
                break

        category_col = None
        for key in keys:
            values = [row.get(key) for row in rows[:60]]
            non_null = [value for value in values if value is not None]
            if not non_null:
                continue
            if all(isinstance(value, str) for value in non_null):
                cardinality = len(set(non_null))
                if 1 < cardinality <= 30:
                    category_col = key
                    break

        if time_col and numeric_cols:
            metric_col = numeric_cols[0]
            picked = rows[:20]
            x_axis = [str(row.get(time_col, ""))[:19] for row in picked]
            data = [float(row.get(metric_col, 0.0) or 0.0) for row in picked]
            if x_axis and data:
                return ChartConfig(
                    chart_type="line",
                    title=f"{metric_col} 时间趋势",
                    x_axis=x_axis,
                    series=[ChartSeries(name=str(metric_col), data=data)],
                )

        if category_col and numeric_cols:
            metric_col = numeric_cols[0]
            buckets: Dict[str, float] = {}
            for row in rows[:300]:
                category = str(row.get(category_col, "unknown"))
                value = row.get(metric_col, 0.0)
                buckets[category] = buckets.get(category, 0.0) + float(value or 0.0)

            top_items = sorted(buckets.items(), key=lambda item: item[1], reverse=True)[:12]
            if top_items:
                return ChartConfig(
                    chart_type="bar",
                    title=f"{category_col} 维度 {metric_col} 分布",
                    x_axis=[key for key, _ in top_items],
                    series=[
                        ChartSeries(
                            name=str(metric_col),
                            data=[value for _, value in top_items],
                        )
                    ],
                )

        if category_col:
            counts: Dict[str, float] = {}
            for row in rows[:300]:
                category = str(row.get(category_col, "unknown"))
                counts[category] = counts.get(category, 0.0) + 1.0

            top_items = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:12]
            if top_items:
                return ChartConfig(
                    chart_type="bar",
                    title=f"{category_col} 频次分布",
                    x_axis=[key for key, _ in top_items],
                    series=[ChartSeries(name="count", data=[value for _, value in top_items])],
                )

        return None

    def _build_text_reply(
        self,
        user_query: str,
        row_count: int,
        rows: List[Dict[str, Any]],
        context_size: int,
    ) -> str:
        preview = rows[:2] if rows else []
        return (
            f"已完成查询与分析。问题: {user_query}。"
            f"命中 {row_count} 行数据，当前上下文条目 {context_size} 条。"
            f"预览: {preview}"
        )
