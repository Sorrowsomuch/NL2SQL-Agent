from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, Optional

from DataAnalyze.agents.base import BaseAgent
from DataAnalyze.config import REVIEWER_LLM_CONFIG
from DataAnalyze.schemas.models import AgentResponse, ReviewDecision
from DataAnalyze.tools.llm_tool import LLMClient, LLMEndpointConfig


class ReviewerAgent(BaseAgent):
    """Reviewer：审核 Executor 结果并给出重试建议。"""

    def __init__(
        self,
        sql_validator: Optional[Callable[[str], tuple[bool, str]]] = None,
        require_chart_for_analysis: bool = True,
        domain_label: str = "database",
    ) -> None:
        super().__init__(name="reviewer")
        # Optional policy hook lets the original reviewer audit Perfetto SQL
        # with the same flow while keeping PostgreSQL behavior unchanged.
        self.sql_validator = sql_validator
        self.require_chart_for_analysis = require_chart_for_analysis
        self.domain_label = domain_label
        self.use_llm_review = REVIEWER_LLM_CONFIG.enabled
        # 当前默认开启 LLM 复核，同时保留规则评审作为硬约束。
        self.use_llm_review = REVIEWER_LLM_CONFIG.enabled
        self._last_llm_error: str = ""
        self._last_llm_raw: str = ""
        self._last_llm_raw_pass1: str = ""
        self._last_llm_raw_pass2: str = ""
        self._last_parse_path: str = ""
        self.llm_client = LLMClient(
            LLMEndpointConfig(
                base_url=REVIEWER_LLM_CONFIG.base_url,
                api_key=REVIEWER_LLM_CONFIG.api_key,
                model=REVIEWER_LLM_CONFIG.model,
                timeout_sec=REVIEWER_LLM_CONFIG.timeout_sec,
            )
        )

    def run(self, response: AgentResponse, user_query: str) -> ReviewDecision:
        rule_decision = self._rule_review(response, user_query)

        if not self.use_llm_review or not self.llm_client.is_enabled():
            return self._attach_debug(rule_decision, mode="rule-only", response=response)

        llm_decision = self._llm_review(response=response, user_query=user_query)
        if llm_decision is None:
            extra = f"，原因: {self._last_llm_error}" if self._last_llm_error else ""
            decision = ReviewDecision(
                approved=rule_decision.approved,
                reason=f"{rule_decision.reason}（LLM 复核不可用，已回退规则评审{extra}）",
                should_retry=rule_decision.should_retry,
            )
            return self._attach_debug(decision, mode="llm-fallback", response=response)

        if not rule_decision.approved:
            decision = ReviewDecision(
                approved=False,
                reason=f"规则评审拒绝: {rule_decision.reason}; LLM意见: {llm_decision.reason}",
                should_retry=rule_decision.should_retry,
            )
            return self._attach_debug(decision, mode="mixed-rule-reject", response=response)

        if not llm_decision.approved:
            if self._is_weak_llm_reject(llm_decision):
                decision = ReviewDecision(
                    approved=rule_decision.approved,
                    reason=f"{rule_decision.reason}（LLM 复核结论不稳定，已回退规则评审: {llm_decision.reason}）",
                    should_retry=rule_decision.should_retry,
                )
                return self._attach_debug(
                    decision,
                    mode="mixed-weak-llm-fallback",
                    response=response,
                )
            decision = ReviewDecision(
                approved=False,
                reason=f"LLM复核未通过: {llm_decision.reason}",
                should_retry=llm_decision.should_retry,
            )
            return self._attach_debug(decision, mode="mixed-llm-reject", response=response)

        decision = ReviewDecision(
            approved=True,
            reason=f"规则+LLM 复核通过（{self._normalize_pass_reason(llm_decision.reason)}）",
            should_retry=False,
        )
        return self._attach_debug(decision, mode="mixed-pass", response=response)

    def _rule_review(self, response: AgentResponse, user_query: str) -> ReviewDecision:
        normalized_query = user_query.lower().strip()

        if not response.success:
            reason = response.error_reason or "Executor 执行失败"
            return ReviewDecision(approved=False, reason=reason, should_retry=True)

        if self.sql_validator is not None:
            is_valid, reason = self.sql_validator(response.sql or "")
            if not is_valid:
                return ReviewDecision(
                    approved=False,
                    reason=f"SQL 不符合 {self.domain_label} 只读策略: {reason}",
                    should_retry=True,
                )
        elif not response.sql or not response.sql.strip().upper().startswith("SELECT"):
            return ReviewDecision(
                approved=False,
                reason="SQL 不符合安全策略（必须以 SELECT 开头）",
                should_retry=True,
            )

        if not response.text_reply.strip():
            return ReviewDecision(
                approved=False,
                reason="回复文本为空，无法返回前端",
                should_retry=True,
            )

        if not response.tool_calls:
            return ReviewDecision(
                approved=False,
                reason="缺少工具调用轨迹，无法审计",
                should_retry=True,
            )

        if (
            self.require_chart_for_analysis
            and self._is_analysis_query(normalized_query)
            and not response.chart
            and self._result_is_chartable(response)
        ):
            return ReviewDecision(
                approved=False,
                reason="分析类请求缺少图表配置",
                should_retry=True,
            )

        if len(response.rows) > 500:
            return ReviewDecision(
                approved=False,
                reason="返回行数过多，建议收敛 LIMIT 后重试",
                should_retry=True,
            )

        if not response.rows and not self._allows_empty_result(normalized_query):
            return ReviewDecision(
                approved=False,
                reason="查询结果为空，建议放宽筛选条件后重试",
                should_retry=True,
            )

        return ReviewDecision(approved=True, reason="审核通过", should_retry=False)

    @staticmethod
    def _is_analysis_query(normalized_query: str) -> bool:
        markers = [
            "分析",
            "趋势",
            "统计",
            "分布",
            "top",
            "chart",
            "trend",
            "distribution",
        ]
        return any(marker in normalized_query for marker in markers)

    @staticmethod
    def _allows_empty_result(normalized_query: str) -> bool:
        markers = [
            "有没有",
            "是否有",
            "确认",
            "检查",
            "没有",
            "无",
            "today no",
            "no error",
            "empty",
        ]
        return any(marker in normalized_query for marker in markers)

    @staticmethod
    def _result_is_chartable(response: AgentResponse) -> bool:
        if not response.rows or not response.columns:
            return False

        if len(response.rows) == 1:
            numeric_count = sum(
                1 for value in response.rows[0].values() if isinstance(value, (int, float))
            )
            return numeric_count >= 2 and len(response.columns) >= 2

        if len(response.columns) < 2:
            return False

        return any(
            isinstance(value, (int, float))
            for row in response.rows[:5]
            for value in row.values()
        )

    def _llm_review(self, response: AgentResponse, user_query: str) -> ReviewDecision | None:
        self._last_llm_error = ""
        self._last_llm_raw = ""
        self._last_llm_raw_pass1 = ""
        self._last_llm_raw_pass2 = ""
        self._last_parse_path = ""

        system_prompt = (
            "你是严格评审器。只允许输出一行，不得有额外解释。"
            "只判断是否违反硬约束：success=true、SQL以SELECT起始、text_reply非空、"
            "tool_calls非空、rows在1到500之间；分析类请求只有在结果明显适合图表时才要求chart。"
            "固定格式: approved=true|false;should_retry=true|false;reason=中文原因(<=40字)。"
        )

        if self.sql_validator is not None:
            system_prompt = (
                f"你是严格审核器，只输出一行。根据 {self.domain_label} 只读 SQL 策略审核结果。"
                "硬约束: success=true、SQL 已通过工具侧只读校验、text_reply 非空、tool_calls 非空、"
                "rows 在 0 到 500 之间；还要判断 SQL 与用户 query 意图是否一致。"
                "固定格式: approved=true|false;should_retry=true|false;reason=中文原因(<=40字)。"
            )

        payload = {
            "query": user_query,
            "text_reply": response.text_reply,
            "sql": response.sql,
            "row_count": len(response.rows),
            "columns": response.columns,
            "findings": response.professional_findings[:5],
            "recommendations": response.recommendations[:5],
            "chart": response.chart.model_dump() if response.chart is not None else None,
            "rows_preview": self._compact_rows(response.rows, max_rows=10),
        }
        user_prompt = "请根据以下结果给出通过/拒绝判定：\n" + json.dumps(
            payload,
            ensure_ascii=False,
            default=str,
        )

        try:
            text = self.llm_client.chat_text(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.0,
                max_tokens=120,
            )
            self._last_llm_raw_pass1 = self._sanitize_excerpt(text)
            self._last_llm_raw = self._last_llm_raw_pass1
            parsed = self._parse_reviewer_text(text)
            if parsed is not None:
                return parsed
            self._last_llm_error = "文本判定格式不符合约定"
        except Exception as ex:
            self._last_llm_error = str(ex)[:160]

        try:
            result = self.llm_client.chat_json(
                system_prompt=(
                    "你是资深评审器。仅输出 JSON："
                    '{"approved":true/false,"should_retry":true/false,"reason":"中文原因"}'
                ),
                user_prompt=user_prompt,
                temperature=0.0,
                max_tokens=200,
            )
            json_excerpt = self._sanitize_excerpt(json.dumps(result, ensure_ascii=False, default=str))
            self._last_llm_raw_pass2 = json_excerpt
            self._last_llm_raw = self._sanitize_excerpt(
                f"[pass1]\n{self._last_llm_raw_pass1}\n[pass2]\n{json_excerpt}",
                520,
            )
            self._last_parse_path = "json-fallback"
            approved = bool(result.get("approved", False))
            reason = str(result.get("reason", "LLM 未给出原因")).strip() or "LLM 未给出原因"
            should_retry = bool(result.get("should_retry", not approved))
            return ReviewDecision(approved=approved, reason=reason, should_retry=should_retry)
        except Exception as ex:
            if self._last_llm_error:
                self._last_llm_error = f"{self._last_llm_error} | {str(ex)[:120]}"
            else:
                self._last_llm_error = str(ex)[:160]
            return None

    def _parse_reviewer_text(self, text: str) -> ReviewDecision | None:
        content = (text or "").strip()
        if not content:
            return None

        json_obj = self._extract_json_obj(content)
        if json_obj is not None:
            approved = self._parse_bool(json_obj.get("approved"))
            should_retry = self._parse_bool(json_obj.get("should_retry"))
            reason = str(json_obj.get("reason", "LLM JSON 评审")).strip() or "LLM JSON 评审"
            if approved is not None:
                self._last_parse_path = "json"
                return ReviewDecision(
                    approved=approved,
                    should_retry=should_retry if should_retry is not None else (not approved),
                    reason=reason,
                )

        normalized = content.replace("；", ";").replace("：", ":")
        approved_match = re.search(r"approved\s*[:=]\s*([^;\n]+)", normalized, flags=re.IGNORECASE)
        retry_match = re.search(r"should_retry\s*[:=]\s*([^;\n]+)", normalized, flags=re.IGNORECASE)
        reason_match = re.search(r"reason\s*[:=]\s*(.+)$", normalized, flags=re.IGNORECASE)
        if approved_match:
            approved = self._parse_bool(approved_match.group(1))
            if approved is None:
                approved = self._infer_approved_from_text(content)
            if approved is not None:
                parsed_retry = self._parse_bool(retry_match.group(1)) if retry_match else None
                reason = reason_match.group(1).strip() if reason_match else "LLM 文本评审"
                self._last_parse_path = "kv"
                return ReviewDecision(
                    approved=approved,
                    should_retry=parsed_retry if parsed_retry is not None else (not approved),
                    reason=reason,
                )

        inferred = self._infer_approved_from_text(content)
        if inferred is not None:
            self._last_parse_path = "infer"
            return ReviewDecision(
                approved=inferred,
                should_retry=not inferred,
                reason=self._trim_reason(content, max_len=80),
            )

        return None

    def _is_weak_llm_reject(self, decision: ReviewDecision) -> bool:
        if decision.approved:
            return False
        reason = (decision.reason or "").strip().lower()
        weak_markers = [
            "llm 文本评审",
            "llm json 评审",
            "llm 未给出原因",
            "unknown",
        ]
        return len(reason) <= 8 or any(marker in reason for marker in weak_markers)

    def _normalize_pass_reason(self, reason: str) -> str:
        text = (reason or "").strip()
        if not text:
            return "满足硬约束"
        return self._trim_reason(text, max_len=60)

    def _trim_reason(self, content: str, max_len: int = 80) -> str:
        compact = re.sub(r"\s+", " ", content).strip()
        if len(compact) <= max_len:
            return compact
        return compact[:max_len] + "..."

    def _attach_debug(
        self,
        decision: ReviewDecision,
        mode: str,
        response: AgentResponse | None = None,
    ) -> ReviewDecision:
        debug: Dict[str, Any] = {
            "mode": mode,
            "llm_enabled": bool(self.use_llm_review and self.llm_client.is_enabled()),
            "parse_path": self._last_parse_path or None,
            "llm_error": self._last_llm_error or None,
            "raw_output_pass1": self._last_llm_raw_pass1 or None,
            "raw_output_pass2": self._last_llm_raw_pass2 or None,
            "raw_output_excerpt": self._last_llm_raw or None,
        }
        if response is not None:
            debug.update(self._extract_schema_debug(response))
        return ReviewDecision(
            approved=decision.approved,
            reason=decision.reason,
            should_retry=decision.should_retry,
            debug=debug,
        )

    def _extract_schema_debug(self, response: AgentResponse) -> Dict[str, Any]:
        schema_debug: Dict[str, Any] = {
            "schema_strategy": None,
            "schema_fetch_mode": None,
            "schema_discovery_tables": None,
            "query_planner_strategy": None,
            "query_planner_reason": None,
            "query_planner_query_type": None,
            "query_planner_primary_metric": None,
            "query_planner_time_requirement": None,
            "query_planner_analysis_dimensions": None,
            "query_planner_filter_dimensions": None,
            "query_planner_candidate_tables_hard": None,
            "query_planner_candidate_tables_soft": None,
            "query_planner_join_needed": None,
            "column_selection_strategy": None,
            "column_planner_strategy": None,
            "column_planner_reason": None,
            "planner_required_columns_by_table": None,
            "planner_optional_columns_by_table": None,
            "selected_columns_by_table": None,
            "schema_tables_used": None,
            "schema_relationships_used": None,
            "prompt_budget": None,
            "knowledge_strategy": None,
            "knowledge_hit_ids": None,
            "knowledge_hit_titles": None,
            "knowledge_column_hints": None,
            "memory_summary_strategy": None,
            "memory_summary_reason": None,
            "memory_fact_strategy": None,
            "memory_fact_reason": None,
            "memory_fact_count": None,
            "sql_guard_outcome": None,
            "sql_guard_reason": None,
        }
        for tool_call in response.tool_calls:
            arguments = tool_call.arguments or {}
            if tool_call.tool_name == "select_schema_context":
                schema_debug["schema_strategy"] = arguments.get("strategy")
                schema_debug["schema_fetch_mode"] = arguments.get("fetch_mode")
                schema_debug["schema_discovery_tables"] = arguments.get("discovery_tables")
                schema_debug["query_planner_strategy"] = arguments.get("query_planner_strategy")
                schema_debug["query_planner_reason"] = arguments.get("query_planner_reason")
                schema_debug["query_planner_query_type"] = arguments.get("query_planner_query_type")
                schema_debug["query_planner_primary_metric"] = arguments.get("query_planner_primary_metric")
                schema_debug["query_planner_time_requirement"] = arguments.get("query_planner_time_requirement")
                schema_debug["query_planner_analysis_dimensions"] = arguments.get("query_planner_analysis_dimensions")
                schema_debug["query_planner_filter_dimensions"] = arguments.get("query_planner_filter_dimensions")
                schema_debug["query_planner_candidate_tables_hard"] = arguments.get(
                    "query_planner_candidate_tables_hard"
                )
                schema_debug["query_planner_candidate_tables_soft"] = arguments.get(
                    "query_planner_candidate_tables_soft"
                )
                schema_debug["query_planner_join_needed"] = arguments.get("query_planner_join_needed")
                schema_debug["column_selection_strategy"] = arguments.get("column_selection_strategy")
                schema_debug["column_planner_strategy"] = arguments.get("column_planner_strategy")
                schema_debug["column_planner_reason"] = arguments.get("column_planner_reason")
                schema_debug["planner_required_columns_by_table"] = arguments.get(
                    "planner_required_columns_by_table"
                )
                schema_debug["planner_optional_columns_by_table"] = arguments.get(
                    "planner_optional_columns_by_table"
                )
                schema_debug["selected_columns_by_table"] = arguments.get("selected_columns_by_table")
                schema_debug["schema_tables_used"] = arguments.get("selected_tables")
                schema_debug["schema_relationships_used"] = arguments.get("selected_relationships")
                schema_debug["prompt_budget"] = arguments.get("prompt_budget_chars")
                schema_debug["knowledge_strategy"] = arguments.get("knowledge_strategy")
                schema_debug["knowledge_hit_ids"] = arguments.get("knowledge_hit_ids")
                schema_debug["knowledge_hit_titles"] = arguments.get("knowledge_hit_titles")
                schema_debug["knowledge_column_hints"] = arguments.get("knowledge_column_hints")
            if tool_call.tool_name == "memory_pipeline":
                schema_debug["memory_summary_strategy"] = arguments.get("summary_strategy")
                schema_debug["memory_summary_reason"] = arguments.get("summary_reason")
                schema_debug["memory_fact_strategy"] = arguments.get("fact_strategy")
                schema_debug["memory_fact_reason"] = arguments.get("fact_reason")
                schema_debug["memory_fact_count"] = arguments.get("fact_count")
            if tool_call.tool_name == "validate_generated_sql":
                schema_debug["sql_guard_outcome"] = arguments.get("outcome")
                schema_debug["sql_guard_reason"] = arguments.get("reason")
        return schema_debug

    def _sanitize_excerpt(self, text: str, max_len: int = 500) -> str:
        content = (text or "").strip()
        if not content:
            return ""

        content = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "sk-***", content)
        content = re.sub(r"(?i)bearer\s+[A-Za-z0-9._-]{8,}", "Bearer ***", content)
        content = re.sub(r"[A-Za-z0-9]{24,}\.[A-Za-z0-9._-]{8,}", "***.***", content)

        if len(content) > max_len:
            return content[:max_len] + "..."
        return content

    def _parse_bool(self, value: Any) -> bool | None:
        if value is None:
            return None
        text = str(value).strip().lower()
        if not text:
            return None

        true_set = {"true", "1", "yes", "y", "通过", "是", "批准", "approve", "approved"}
        false_set = {"false", "0", "no", "n", "不通过", "否", "拒绝", "reject", "rejected"}
        if text in true_set:
            return True
        if text in false_set:
            return False
        if "通过" in text and "不通过" not in text:
            return True
        if "不通过" in text or "拒绝" in text:
            return False
        return None

    def _extract_json_obj(self, content: str) -> dict | None:
        blocks = re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", content, flags=re.IGNORECASE)
        candidates = blocks + [content]
        for candidate in candidates:
            text = candidate.strip()
            try:
                obj = json.loads(text)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass

            start = text.find("{")
            end = text.rfind("}")
            if 0 <= start < end:
                fragment = text[start : end + 1]
                try:
                    obj = json.loads(fragment)
                    if isinstance(obj, dict):
                        return obj
                except Exception:
                    pass
        return None

    def _infer_approved_from_text(self, content: str) -> bool | None:
        text = (content or "").strip()
        if not text:
            return None

        deny_tokens = ["不通过", "拒绝", "应重试", "需要重试", "不满足", "失败"]
        pass_tokens = ["审核通过", "通过", "可通过", "可发布", "满足要求", "无需重试"]
        denied = any(token in text for token in deny_tokens)
        passed = any(token in text for token in pass_tokens)
        if denied and not passed:
            return False
        if passed and not denied:
            return True
        return None

    def _compact_rows(self, rows: list[dict], max_rows: int = 10) -> list[dict]:
        compact: list[dict] = []
        for row in rows[:max_rows]:
            item: dict = {}
            for key, value in row.items():
                safe_key = str(key)[:48]
                item[safe_key] = self._compact_value(value)
            compact.append(item)
        return compact

    def _compact_value(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (int, float, bool)):
            return value
        text = str(value)
        if len(text) > 160:
            return text[:160] + "..."
        return text
