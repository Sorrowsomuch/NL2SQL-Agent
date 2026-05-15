from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple

from DataAnalyze.schemas.models import DatabaseSchemaMetadata
from DataAnalyze.tools.db.knowledge_retrieval import KnowledgeHit


QUERY_TYPES = {"trend", "aggregate", "topn", "detail", "distribution", "lookup"}
TIME_REQUIREMENTS = {"required", "optional", "none"}


def build_query_planner_prompt_context(
    metadata: DatabaseSchemaMetadata,
    knowledge_hits: List[KnowledgeHit],
) -> str:
    """构造第一阶段 query planner 的输入上下文。"""

    lines: List[str] = ["候选表清单："]
    for table in metadata.tables[:20]:
        parts = [table.name]
        if table.semantic_hint:
            parts.append(f"purpose={table.semantic_hint}")
        if table.row_grain:
            parts.append(f"grain={table.row_grain}")
        lines.append(f"- {' | '.join(parts)}")

    if knowledge_hits:
        lines.append("相关知识：")
        for hit in knowledge_hits[:8]:
            target = ", ".join(hit.related_tables) if hit.related_tables else "general"
            lines.append(f"- [{hit.kind}] {hit.title} | {target} | {hit.summary}")

    return "\n".join(lines)


def sanitize_query_plan_output(
    metadata: DatabaseSchemaMetadata,
    raw_plan: Dict[str, Any],
) -> tuple[Dict[str, Any], List[str]]:
    """清洗第一阶段 planner 输出，只保留系统可接受的结果。"""

    allowed_tables = {table.name for table in metadata.tables}
    notes: List[str] = []

    query_type = _normalize_enum(
        raw_plan.get("query_type"),
        QUERY_TYPES,
        default="detail",
        notes=notes,
        label="query_type",
    )
    time_requirement = _normalize_enum(
        raw_plan.get("time_requirement"),
        TIME_REQUIREMENTS,
        default="optional",
        notes=notes,
        label="time_requirement",
    )
    primary_metric = str(raw_plan.get("primary_metric", "") or "").strip()
    analysis_dimensions = _normalize_string_list(raw_plan.get("analysis_dimensions"))
    filter_dimensions = _normalize_string_list(raw_plan.get("filter_dimensions"))
    candidate_tables_hard = _sanitize_table_list(
        raw_plan.get("candidate_tables_hard"),
        allowed_tables,
        notes,
        "candidate_tables_hard",
    )
    candidate_tables_soft = _sanitize_table_list(
        raw_plan.get("candidate_tables_soft"),
        allowed_tables,
        notes,
        "candidate_tables_soft",
    )
    candidate_tables_soft = [
        table_name
        for table_name in candidate_tables_soft
        if table_name not in set(candidate_tables_hard)
    ]
    join_needed = bool(raw_plan.get("join_needed", False))
    reason = str(raw_plan.get("reason", "") or "").strip() or "query_planner_success"

    return (
        {
            "query_type": query_type,
            "primary_metric": primary_metric,
            "time_requirement": time_requirement,
            "analysis_dimensions": analysis_dimensions,
            "filter_dimensions": filter_dimensions,
            "candidate_tables_hard": candidate_tables_hard,
            "candidate_tables_soft": candidate_tables_soft,
            "join_needed": join_needed,
            "reason": reason,
        },
        notes,
    )


def merge_query_planner_tables(
    ranked_tables: List[str],
    hard_tables: List[str],
    soft_tables: List[str],
    limit: int,
) -> List[str]:
    """把 planner 的 hard/soft 结果和现有排序结果合并成稳定顺序。"""

    merged: List[str] = []
    seen = set()

    def add_many(values: Iterable[str]) -> None:
        for value in values:
            if not value or value in seen:
                continue
            merged.append(value)
            seen.add(value)
            if len(merged) >= limit:
                return

    add_many(hard_tables)
    if len(merged) < limit:
        add_many(soft_tables)
    if len(merged) < limit:
        add_many(ranked_tables)
    return merged[:limit]


def build_query_plan_summary(query_plan: Dict[str, Any]) -> str:
    """为第二阶段字段 planner 生成简短的上游规划摘要。"""

    parts: List[str] = []
    query_type = str(query_plan.get("query_type", "") or "").strip()
    if query_type:
        parts.append(f"query_type={query_type}")
    primary_metric = str(query_plan.get("primary_metric", "") or "").strip()
    if primary_metric:
        parts.append(f"primary_metric={primary_metric}")
    analysis_dimensions = query_plan.get("analysis_dimensions") or []
    if analysis_dimensions:
        parts.append(f"analysis_dimensions={analysis_dimensions}")
    filter_dimensions = query_plan.get("filter_dimensions") or []
    if filter_dimensions:
        parts.append(f"filter_dimensions={filter_dimensions}")
    parts.append(f"join_needed={bool(query_plan.get('join_needed', False))}")
    return " | ".join(parts)


def _normalize_enum(
    raw_value: Any,
    allowed_values: set[str],
    default: str,
    notes: List[str],
    label: str,
) -> str:
    value = str(raw_value or "").strip().lower()
    if value in allowed_values:
        return value
    if value:
        notes.append(f"Query planner {label} ignored: {value}.")
    return default


def _normalize_string_list(raw_value: Any) -> List[str]:
    if not isinstance(raw_value, list):
        return []
    cleaned: List[str] = []
    seen = set()
    for item in raw_value:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        cleaned.append(value)
        seen.add(value)
    return cleaned


def _sanitize_table_list(
    raw_value: Any,
    allowed_tables: set[str],
    notes: List[str],
    label: str,
) -> List[str]:
    cleaned: List[str] = []
    seen = set()
    if not isinstance(raw_value, list):
        return cleaned
    for item in raw_value:
        table_name = str(item or "").strip()
        if not table_name:
            continue
        if table_name not in allowed_tables:
            notes.append(f"Query planner {label} ignored: {table_name}.")
            continue
        if table_name in seen:
            continue
        cleaned.append(table_name)
        seen.add(table_name)
    return cleaned
