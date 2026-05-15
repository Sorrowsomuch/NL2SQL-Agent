from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from DataAnalyze.config import DB_CONFIG, EXECUTOR_LLM_CONFIG
from DataAnalyze.middleware.metrics import (
    observe_memory_compression,
    observe_memory_fact_extraction,
)
from DataAnalyze.schemas.models import MemoryLayer, MemoryType
from DataAnalyze.tools.llm_tool import LLMClient, LLMEndpointConfig


@dataclass
class MemoryRecord:
    """会话记忆记录。"""

    db_id: Optional[int]
    role: str
    content: str
    memory_layer: MemoryLayer
    memory_type: MemoryType
    created_at: datetime = field(default_factory=datetime.utcnow)
    compressed: bool = False
    salience_score: float = 0.0
    source_range: Optional[Tuple[int, int]] = None


class MemoryManager:
    """分层记忆管理器，负责装载、压缩、事实提取与持久化。"""

    COMPRESS_KEEP_RECENT_RAW = 10

    def __init__(self) -> None:
        self._store: Dict[str, List[MemoryRecord]] = {}
        self._loaded_sessions: Set[str] = set()
        self._last_debug_by_session: Dict[str, Dict[str, Any]] = {}
        self._persist_enabled = os.getenv("DATAANALYZE_MEMORY_PERSIST", "true").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.db_host = os.getenv("DATAANALYZE_DB_HOST", DB_CONFIG.host)
        self.db_port = int(os.getenv("DATAANALYZE_DB_PORT", str(DB_CONFIG.port)))
        self.db_name = os.getenv("DATAANALYZE_DB_NAME", DB_CONFIG.name)
        self.db_user = os.getenv("DATAANALYZE_DB_USER", DB_CONFIG.user)
        self.db_password = os.getenv("DATAANALYZE_DB_PASSWORD", DB_CONFIG.password)
        self.llm_client = LLMClient(
            LLMEndpointConfig(
                base_url=EXECUTOR_LLM_CONFIG.base_url,
                api_key=EXECUTOR_LLM_CONFIG.api_key,
                model=EXECUTOR_LLM_CONFIG.model,
                timeout_sec=EXECUTOR_LLM_CONFIG.timeout_sec,
            )
        )

    def _connect_postgres(self) -> Optional[Any]:
        if not self._persist_enabled:
            return None

        try:
            import psycopg
        except Exception:
            return None

        try:
            return psycopg.connect(
                host=self.db_host,
                port=self.db_port,
                dbname=self.db_name,
                user=self.db_user,
                password=self.db_password,
            )
        except Exception:
            return None

    def _ensure_loaded(self, session_id: str) -> None:
        """按 session 懒加载数据库记忆到进程内存。"""
        if session_id in self._loaded_sessions:
            return

        self._store.setdefault(session_id, [])
        conn = self._connect_postgres()
        if conn is None:
            self._loaded_sessions.add(session_id)
            return

        query = """
            SELECT id, role, memory_layer, memory_type, content, compressed,
                   COALESCE(salience_score, 0), source_range_start, source_range_end, created_at
            FROM chat_memories
            WHERE session_id = %s
            ORDER BY id ASC
        """
        try:
            with conn.cursor() as cur:
                cur.execute(query, (session_id,))
                rows = cur.fetchall()

            records: List[MemoryRecord] = []
            for row in rows:
                records.append(
                    MemoryRecord(
                        db_id=int(row[0]),
                        role=str(row[1]),
                        memory_layer=str(row[2]),
                        memory_type=str(row[3]),
                        content=str(row[4]),
                        compressed=bool(row[5]),
                        salience_score=float(row[6] or 0.0),
                        source_range=(int(row[7]), int(row[8])) if row[7] and row[8] else None,
                        created_at=row[9] if row[9] else datetime.utcnow(),
                    )
                )
            self._store[session_id] = records
        except Exception:
            # 数据库装载失败时静默退回纯内存模式，不阻断主流程。
            pass
        finally:
            conn.close()
            self._loaded_sessions.add(session_id)

    def _upsert_session(self, conn: Any, session_id: str) -> None:
        query = """
            INSERT INTO chat_sessions (session_id, title, created_at, updated_at)
            VALUES (%s, %s, NOW(), NOW())
            ON CONFLICT (session_id)
            DO UPDATE SET updated_at = EXCLUDED.updated_at
        """
        with conn.cursor() as cur:
            cur.execute(query, (session_id, ""))

    def _persist_record(self, session_id: str, record: MemoryRecord) -> Optional[int]:
        conn = self._connect_postgres()
        if conn is None:
            return None

        insert_query = """
            INSERT INTO chat_memories (
                session_id, role, memory_layer, memory_type, content,
                compressed, salience_score, source_range_start, source_range_end, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            RETURNING id
        """
        try:
            self._upsert_session(conn, session_id)
            with conn.cursor() as cur:
                cur.execute(
                    insert_query,
                    (
                        session_id,
                        record.role,
                        record.memory_layer,
                        record.memory_type,
                        record.content,
                        record.compressed,
                        record.salience_score,
                        record.source_range[0] if record.source_range else None,
                        record.source_range[1] if record.source_range else None,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
            return int(row[0]) if row else None
        except Exception:
            conn.rollback()
            return None
        finally:
            conn.close()

    def _mark_records_compressed(self, ids: List[int]) -> None:
        if not ids:
            return
        conn = self._connect_postgres()
        if conn is None:
            return
        query = "UPDATE chat_memories SET compressed = TRUE WHERE id = ANY(%s)"
        try:
            with conn.cursor() as cur:
                cur.execute(query, (ids,))
            conn.commit()
        except Exception:
            conn.rollback()
        finally:
            conn.close()

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        layer: MemoryLayer = "L0_RAW",
        memory_type: MemoryType = "raw",
    ) -> None:
        if not content.strip():
            return

        self._ensure_loaded(session_id)
        records = self._store.setdefault(session_id, [])
        record = MemoryRecord(
            db_id=None,
            role=role,
            content=content.strip(),
            memory_layer=layer,
            memory_type=memory_type,
        )
        record.db_id = self._persist_record(session_id, record)
        records.append(record)

    def get_layer_messages(
        self, session_id: str, layer: MemoryLayer, limit: int
    ) -> List[MemoryRecord]:
        self._ensure_loaded(session_id)
        records = [record for record in self._store.get(session_id, []) if record.memory_layer == layer]
        if limit <= 0:
            return records
        return records[-limit:]

    def get_session_memory(
        self,
        session_id: str,
        limit: int = 200,
        layer: Optional[MemoryLayer] = None,
        include_compressed: bool = True,
    ) -> List[MemoryRecord]:
        """返回会话记忆轨迹，可按层过滤。"""
        self._ensure_loaded(session_id)
        records = list(self._store.get(session_id, []))
        if layer is not None:
            records = [record for record in records if record.memory_layer == layer]
        if not include_compressed:
            records = [record for record in records if not record.compressed]
        if limit <= 0:
            return records
        return records[-limit:]

    def list_sessions(self, limit: int = 200) -> List[str]:
        """列出当前可见会话。"""
        sessions = set(self._store.keys())
        conn = self._connect_postgres()
        if conn is not None:
            query = "SELECT session_id FROM chat_sessions ORDER BY updated_at DESC LIMIT %s"
            try:
                with conn.cursor() as cur:
                    cur.execute(query, (limit,))
                    rows = cur.fetchall()
                sessions.update(str(row[0]) for row in rows if row and row[0])
            except Exception:
                pass
            finally:
                conn.close()

        ordered = sorted(sessions)
        if limit <= 0:
            return ordered
        return ordered[:limit]

    def build_prompt_context(
        self,
        session_id: str,
        user_input: str,
        l0_window: int = 10,
        fact_top_k: int = 8,
        summary_keep_count: int = 4,
    ) -> List[MemoryRecord]:
        """按 L1 -> L2 -> L0 -> 当前用户输入 的顺序拼装 prompt 上下文。"""
        self._ensure_loaded(session_id)
        prompt_records: List[MemoryRecord] = []

        summaries = self.get_layer_messages(session_id, "L1_SUMMARY", summary_keep_count)
        prompt_records.extend(summaries)

        fact_pool = self.get_layer_messages(session_id, "L2_FACT", max(fact_top_k * 5, 40))
        prompt_records.extend(self._rerank_facts_by_query(fact_pool, user_input, fact_top_k))

        raws = self.get_layer_messages(session_id, "L0_RAW", l0_window)
        prompt_records.extend(raws)

        prompt_records.append(
            MemoryRecord(
                db_id=None,
                role="user",
                content=user_input,
                memory_layer="L0_RAW",
                memory_type="raw",
            )
        )
        return prompt_records

    def compress_context(
        self,
        session_id: str,
        token_budget: int = 6000,
        reserve_output_tokens: int = 1200,
        trigger_ratio: int = 75,
    ) -> Optional[str]:
        """压缩旧的 L0 原始消息，优先使用 LLM 摘要，失败时回退规则摘要。"""
        self._ensure_loaded(session_id)
        raw_records = [
            record
            for record in self._store.get(session_id, [])
            if record.memory_layer == "L0_RAW" and not record.compressed
        ]
        if not raw_records:
            self._update_debug(session_id, memory_summary_strategy="skipped", memory_summary_reason="no_raw_records")
            observe_memory_compression(strategy="skipped", outcome="no_raw_records")
            return None

        raw_tokens = sum(self._estimate_tokens(record.content) for record in raw_records)
        usable = max(1000, token_budget - reserve_output_tokens)
        trigger = usable * trigger_ratio // 100
        if raw_tokens < trigger:
            self._update_debug(session_id, memory_summary_strategy="skipped", memory_summary_reason="below_trigger")
            observe_memory_compression(strategy="skipped", outcome="below_trigger")
            return None

        if len(raw_records) <= self.COMPRESS_KEEP_RECENT_RAW:
            self._update_debug(session_id, memory_summary_strategy="skipped", memory_summary_reason="keep_recent_only")
            observe_memory_compression(strategy="skipped", outcome="keep_recent_only")
            return None

        candidates = raw_records[: -self.COMPRESS_KEEP_RECENT_RAW]
        if not candidates:
            self._update_debug(session_id, memory_summary_strategy="skipped", memory_summary_reason="no_candidates")
            observe_memory_compression(strategy="skipped", outcome="no_candidates")
            return None

        summary = self._build_summary_with_llm(candidates)
        summary_strategy = "llm"
        if not summary:
            summary = self._build_structured_summary_fallback(candidates)
            summary_strategy = "fallback"

        if not summary:
            self._update_debug(session_id, memory_summary_strategy="failed", memory_summary_reason="empty_summary")
            observe_memory_compression(strategy="failed", outcome="empty_summary")
            return None

        start_ref = candidates[0].db_id if candidates[0].db_id is not None else 0
        end_ref = candidates[-1].db_id if candidates[-1].db_id is not None else 0

        for record in candidates:
            record.compressed = True

        self._mark_records_compressed(
            [int(record.db_id) for record in candidates if record.db_id is not None]
        )

        summary_record = MemoryRecord(
            db_id=None,
            role="system",
            content=summary,
            memory_layer="L1_SUMMARY",
            memory_type="summary",
            compressed=True,
            source_range=(start_ref, end_ref),
        )
        summary_record.db_id = self._persist_record(session_id, summary_record)
        self._store[session_id].append(summary_record)

        self._update_debug(
            session_id,
            memory_summary_strategy=summary_strategy,
            memory_summary_reason="success",
            memory_summary_source_count=len(candidates),
        )
        observe_memory_compression(strategy=summary_strategy, outcome="success")
        return summary

    def extract_facts_if_needed(self, session_id: str, max_facts: int = 8) -> List[str]:
        """从最近原始消息中提取稳定事实，优先使用 LLM 结构化抽取，失败时回退规则抽取。"""
        self._ensure_loaded(session_id)
        latest = self.get_layer_messages(session_id, "L0_RAW", 30)
        if not latest:
            self._update_debug(session_id, memory_fact_strategy="skipped", memory_fact_reason="no_raw_records")
            observe_memory_fact_extraction(strategy="skipped", outcome="no_raw_records", count=0)
            return []

        existing = {
            record.content.strip()
            for record in self.get_layer_messages(session_id, "L2_FACT", 200)
            if record.content.strip()
        }

        llm_facts = self._extract_facts_with_llm(latest, max_facts=max_facts)
        fact_items = llm_facts if llm_facts else self._extract_facts_fallback(latest, max_facts=max_facts)
        fact_strategy = "llm" if llm_facts else "fallback"

        created: List[str] = []
        seen = set()
        for item in fact_items:
            content = str(item.get("content", "")).strip()
            if len(content) < 8:
                continue
            if content in existing or content in seen:
                continue
            seen.add(content)

            record = MemoryRecord(
                db_id=None,
                role="system",
                content=content,
                memory_layer="L2_FACT",
                memory_type="fact",
                salience_score=float(item.get("salience_score", 0.8) or 0.8),
            )
            record.db_id = self._persist_record(session_id, record)
            self._store[session_id].append(record)
            created.append(content)
            if len(created) >= max_facts:
                break

        outcome = "success" if created else "no_new_fact"
        self._update_debug(
            session_id,
            memory_fact_strategy=fact_strategy,
            memory_fact_reason=outcome,
            memory_fact_count=len(created),
        )
        observe_memory_fact_extraction(strategy=fact_strategy, outcome=outcome, count=len(created))
        return created

    def get_last_debug(self, session_id: str) -> Dict[str, Any]:
        return dict(self._last_debug_by_session.get(session_id, {}))

    def _build_summary_with_llm(self, records: List[MemoryRecord]) -> Optional[str]:
        if not self.llm_client.is_enabled():
            return None

        messages = [
            {"role": record.role, "content": record.content[:500]}
            for record in records[-24:]
        ]
        system_prompt = (
            "你是会话记忆压缩器。"
            "请把历史对话压缩成结构化摘要，只输出 JSON。"
            "格式为："
            "{\"summary\":\"...\",\"user_goals\":[\"...\"],\"constraints\":[\"...\"],"
            "\"completed\":[\"...\"],\"todo\":[\"...\"],\"reusable_facts\":[\"...\"]}"
        )
        user_prompt = (
            "请总结下面这些较旧的对话片段，保留后续继续完成任务所需的信息，避免废话。\n"
            f"{messages}"
        )
        try:
            result = self.llm_client.chat_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.1,
                max_tokens=1000,
            )
        except Exception:
            return None

        summary = str(result.get("summary", "")).strip()
        sections = [
            self._render_summary_section("1. 会话摘要", [summary] if summary else []),
            self._render_summary_section("2. 用户目标", result.get("user_goals")),
            self._render_summary_section("3. 关键约束", result.get("constraints")),
            self._render_summary_section("4. 已完成事项", result.get("completed")),
            self._render_summary_section("5. 待完成事项", result.get("todo")),
            self._render_summary_section("6. 可复用事实", result.get("reusable_facts")),
        ]
        rendered = "\n\n".join(section for section in sections if section)
        return rendered.strip() or None

    def _extract_facts_with_llm(
        self,
        records: List[MemoryRecord],
        max_facts: int,
    ) -> List[Dict[str, Any]]:
        if not self.llm_client.is_enabled():
            return []

        payload = [
            {"role": record.role, "content": record.content[:500]}
            for record in records[-20:]
        ]
        system_prompt = (
            "你是稳定事实提取器。"
            "请从对话中抽取后续仍然有价值的稳定事实，只输出 JSON。"
            "格式为："
            "{\"facts\":[{\"content\":\"...\",\"salience_score\":0.0,\"fact_type\":\"constraint|goal|preference|environment|data\"}]}"
            "不要提取短期推测，不要提取临时错误，不要重复。"
        )
        user_prompt = (
            f"请从下面的会话中抽取最多 {max_facts} 条稳定事实：\n"
            f"{payload}"
        )
        try:
            result = self.llm_client.chat_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.1,
                max_tokens=900,
            )
        except Exception:
            return []

        facts = result.get("facts")
        if not isinstance(facts, list):
            return []

        normalized: List[Dict[str, Any]] = []
        for item in facts:
            if isinstance(item, str):
                normalized.append({"content": item, "salience_score": 0.8})
                continue
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            salience = item.get("salience_score", 0.8)
            try:
                salience_value = max(0.0, min(1.0, float(salience)))
            except Exception:
                salience_value = 0.8
            normalized.append(
                {
                    "content": content,
                    "salience_score": salience_value,
                    "fact_type": str(item.get("fact_type", "")),
                }
            )
        return normalized[:max_facts]

    def _build_structured_summary_fallback(self, records: List[MemoryRecord]) -> str:
        user_goals: List[str] = []
        constraints: List[str] = []
        completed: List[str] = []
        todo: List[str] = []

        for record in records:
            text = record.content.strip()
            if not text:
                continue
            if record.role == "user":
                user_goals.append(text)
            if any(word in text for word in ["必须", "仅", "禁止", "约束", "强制"]):
                constraints.append(text)
            if any(word in text.lower() for word in ["已完成", "完成", "done", "通过"]):
                completed.append(text)
            if any(word in text.lower() for word in ["待", "下一步", "todo", "需要"]):
                todo.append(text)

        reusable_facts = self._extract_reusable_facts(records)
        sections = [
            self._render_summary_section("1. 用户目标", self._tail_or_default(user_goals)),
            self._render_summary_section("2. 关键约束", self._tail_or_default(constraints)),
            self._render_summary_section("3. 已完成事项", self._tail_or_default(completed)),
            self._render_summary_section("4. 待完成事项", self._tail_or_default(todo)),
            self._render_summary_section("5. 可复用事实", reusable_facts or ["无"]),
        ]
        return "\n\n".join(sections)

    def _extract_facts_fallback(
        self,
        records: List[MemoryRecord],
        max_facts: int,
    ) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        for record in records:
            text = record.content.strip()
            if len(text) < 8:
                continue
            if "当前时间" in text or re.search(r"\d{4}-\d{2}-\d{2}", text):
                continue
            if any(token in text for token in ["必须", "需要", "约束", "固定", "仅允许"]):
                candidates.append({"content": text, "salience_score": 0.8})
            if len(candidates) >= max_facts:
                break
        return candidates

    def _extract_reusable_facts(self, records: List[MemoryRecord]) -> List[str]:
        facts: List[str] = []
        for record in records:
            text = record.content.strip()
            if 8 <= len(text) <= 120 and any(key in text for key in ["接口", "字段", "流程", "SQL"]):
                facts.append(text)
        return facts[-5:]

    def _rerank_facts_by_query(
        self, facts: List[MemoryRecord], query: str, top_k: int
    ) -> List[MemoryRecord]:
        if not facts:
            return []

        normalized_query = self._normalize_text(query)
        if not normalized_query:
            return facts[-top_k:]

        query_terms = self._tokenize(normalized_query)
        scored = [
            (fact, self._score_fact(fact.content, normalized_query, query_terms))
            for fact in facts
        ]
        scored = [item for item in scored if item[1] > 0.0]
        scored.sort(key=lambda item: item[1], reverse=True)
        return [item[0] for item in scored[:top_k]]

    def _score_fact(self, content: str, query: str, query_terms: Set[str]) -> float:
        normalized = self._normalize_text(content)
        if not normalized:
            return 0.0

        score = 4.0 if query in normalized else 0.0
        overlap = sum(1 for term in query_terms if term in normalized)
        if query_terms:
            score += 3.0 * overlap / len(query_terms)
        score -= min(len(normalized) / 400.0, 0.8)
        return score

    def _update_debug(self, session_id: str, **kwargs: Any) -> None:
        debug = dict(self._last_debug_by_session.get(session_id, {}))
        debug.update(kwargs)
        self._last_debug_by_session[session_id] = debug

    def _render_summary_section(self, title: str, items: Optional[List[Any]]) -> str:
        normalized_items = [str(item).strip() for item in (items or []) if str(item).strip()]
        if not normalized_items:
            normalized_items = ["无"]
        return title + "\n" + "\n".join(f"- {item}" for item in normalized_items[:6])

    def _tail_or_default(self, items: List[str], n: int = 5) -> List[str]:
        return items[-n:] if items else ["无"]

    @staticmethod
    def _tokenize(text: str) -> Set[str]:
        return {
            token.lower()
            for token in re.split(r"[^\u4e00-\u9fa5a-zA-Z0-9]+", text)
            if len(token.strip()) >= 2
        }

    @staticmethod
    def _normalize_text(text: str) -> str:
        return text.strip().lower() if text else ""

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        if not text:
            return 0
        return math.ceil((len(text) / 4.0) * 2.2)
