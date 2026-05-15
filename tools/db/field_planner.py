from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from DataAnalyze.schemas.models import DatabaseSchemaMetadata
from DataAnalyze.tools.db.knowledge_retrieval import KnowledgeHit


def build_planner_prompt_context(
    metadata: DatabaseSchemaMetadata,
    selected_tables: List[str],
    column_hits: List[KnowledgeHit],
    planning_hits: List[KnowledgeHit],
    query_plan_summary: str = "",
) -> str:
    """构造字段 planner 的输入上下文，尽量短而可解释。"""

    selected = set(selected_tables)
    lines: List[str] = ["候选表与字段："]
    for table in metadata.tables:
        if table.name not in selected:
            continue
        column_parts = ", ".join(
            f"{column.name}:{column.data_type}" for column in table.columns[:20]
        )
        lines.append(f"- {table.name}: {column_parts}")

    if planning_hits:
        lines.append("知识提示：")
        for hit in planning_hits[:8]:
            target = ", ".join(hit.related_tables) if hit.related_tables else "general"
            if hit.kind == "column_semantics" and hit.table_name and hit.column_name:
                target = f"{hit.table_name}.{hit.column_name}"
            lines.append(f"- [{hit.kind}] {hit.title} | {target} | {hit.summary}")

    if column_hits:
        lines.append("字段语义优先提示：")
        for hit in column_hits[:8]:
            if hit.table_name and hit.column_name:
                lines.append(f"- {hit.table_name}.{hit.column_name}: {hit.summary}")

    if query_plan_summary:
        lines.append("上游 query 规划：")
        lines.append(f"- {query_plan_summary}")

    return "\n".join(lines)


def sanitize_planner_output(
    metadata: DatabaseSchemaMetadata,
    selected_tables: List[str],
    raw_required: Optional[Dict[str, Any]],
    raw_optional: Optional[Dict[str, Any]],
) -> tuple[Dict[str, List[str]], Dict[str, List[str]], List[str]]:
    """
    对 planner 输出做系统约束收敛。

    这里负责保证：
    - 字段真实存在
    - 字段属于候选表
    - 去重且顺序稳定
    """

    selected = set(selected_tables)
    columns_by_table = {
        table.name: {column.name for column in table.columns}
        for table in metadata.tables
        if table.name in selected
    }
    notes: List[str] = []
    required = _sanitize_column_map(raw_required, columns_by_table, notes, "required")
    optional = _sanitize_column_map(raw_optional, columns_by_table, notes, "optional")
    return required, optional, notes


def build_final_column_priorities(
    metadata: DatabaseSchemaMetadata,
    selected_tables: List[str],
    base_priorities: Dict[str, List[str]],
    planner_required: Dict[str, List[str]],
    planner_optional: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    """
    把系统保底列、planner 结果和现有 query-aware 优先级合并成最终列顺序。

    顺序策略：
    1. 系统保底列：主键、关系列、时间列
    2. planner 必选列
    3. 现有 query-aware 优先级列
    4. planner 可选列
    """

    selected = set(selected_tables)
    relation_columns = _collect_relation_columns(metadata, selected)
    final_priorities: Dict[str, List[str]] = {}

    for table in metadata.tables:
        if table.name not in selected:
            continue
        ordered: List[str] = []
        seen = set()

        def add_many(values: Iterable[str]) -> None:
            for value in values:
                if not value or value in seen:
                    continue
                ordered.append(value)
                seen.add(value)

        time_columns = [
            column.name for column in table.columns if _is_time_like_column(column.name)
        ]
        add_many(table.primary_key)
        add_many(relation_columns.get(table.name, []))
        add_many(time_columns)
        add_many(planner_required.get(table.name, []))
        add_many(base_priorities.get(table.name, []))
        add_many(planner_optional.get(table.name, []))
        final_priorities[table.name] = ordered

    return final_priorities


def _sanitize_column_map(
    raw_map: Optional[Dict[str, Any]],
    columns_by_table: Dict[str, set[str]],
    notes: List[str],
    label: str,
) -> Dict[str, List[str]]:
    if not isinstance(raw_map, dict):
        return {}

    cleaned: Dict[str, List[str]] = {}
    for table_name, raw_columns in raw_map.items():
        if table_name not in columns_by_table:
            notes.append(f"Planner {label} table ignored: {table_name}.")
            continue
        if not isinstance(raw_columns, list):
            notes.append(f"Planner {label} columns ignored for {table_name}: not a list.")
            continue

        ordered: List[str] = []
        seen = set()
        allowed_columns = columns_by_table[table_name]
        for item in raw_columns:
            column_name = str(item or "").strip()
            if not column_name:
                continue
            if column_name not in allowed_columns:
                notes.append(f"Planner {label} column ignored: {table_name}.{column_name}.")
                continue
            if column_name in seen:
                continue
            ordered.append(column_name)
            seen.add(column_name)
        if ordered:
            cleaned[table_name] = ordered
    return cleaned


def _collect_relation_columns(
    metadata: DatabaseSchemaMetadata,
    selected_tables: set[str],
) -> Dict[str, List[str]]:
    relation_columns: Dict[str, List[str]] = {}
    for relation in metadata.relationships:
        if relation.from_table in selected_tables and relation.to_table in selected_tables:
            relation_columns.setdefault(relation.from_table, []).extend(relation.from_columns)
            relation_columns.setdefault(relation.to_table, []).extend(relation.to_columns)
    return relation_columns


def _is_time_like_column(column_name: str) -> bool:
    normalized = column_name.lower()
    return any(marker in normalized for marker in ["ts", "time", "date", "created", "updated"])
