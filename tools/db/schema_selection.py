from __future__ import annotations

import re
from typing import Dict, List, Optional

from DataAnalyze.schemas.models import DatabaseSchemaMetadata, SchemaRelationship, TableSchema
from DataAnalyze.tools.db.knowledge_retrieval import KnowledgeHit, KnowledgeRetrievalResult


def to_inventory_metadata(
    metadata: DatabaseSchemaMetadata,
    note: str,
) -> DatabaseSchemaMetadata:
    """把详细 schema 压成轻量表清单，供第一段候选发现使用。"""

    return DatabaseSchemaMetadata(
        source=metadata.source,
        schema_name=metadata.schema_name,
        tables=[
            TableSchema(
                name=table.name,
                description=table.description,
                semantic_hint=table.semantic_hint,
                row_grain=table.row_grain,
            )
            for table in metadata.tables
        ],
        notes=list(metadata.notes) + [note],
        generated_at=metadata.generated_at,
    )


def apply_project_table_scope(
    metadata: DatabaseSchemaMetadata,
    scope_enabled: bool,
    allowlist: List[str],
    denylist: List[str],
) -> DatabaseSchemaMetadata:
    """
    在项目边界内收缩 schema 候选。

    这是工程边界控制，不是智能检索逻辑，所以单独抽出来便于审计。
    """

    if not scope_enabled:
        return metadata

    scoped_tables = list(metadata.tables)
    notes = list(metadata.notes)

    if allowlist:
        allow_set = set(allowlist)
        allow_filtered = [table for table in scoped_tables if table.name in allow_set]
        if allow_filtered:
            removed = len(scoped_tables) - len(allow_filtered)
            scoped_tables = allow_filtered
            if removed > 0:
                notes.append(
                    f"Project allowlist kept {len(scoped_tables)} table(s) and filtered {removed} unrelated table(s)."
                )
        else:
            notes.append("Project allowlist matched no tables, so the original metadata scope was kept.")

    if denylist:
        deny_set = set(denylist)
        before = len(scoped_tables)
        scoped_tables = [table for table in scoped_tables if table.name not in deny_set]
        removed = before - len(scoped_tables)
        if removed > 0:
            notes.append(f"Project denylist filtered {removed} table(s) from schema candidates.")

    scoped_names = {table.name for table in scoped_tables}
    scoped_relationships = [
        relation
        for relation in metadata.relationships
        if relation.from_table in scoped_names and relation.to_table in scoped_names
    ]

    return DatabaseSchemaMetadata(
        source=metadata.source,
        schema_name=metadata.schema_name,
        tables=scoped_tables,
        relationships=scoped_relationships,
        notes=notes,
        generated_at=metadata.generated_at,
    )


def rank_tables_for_query(
    user_query: str,
    metadata: DatabaseSchemaMetadata,
    query_term_hints: Dict[str, List[str]],
    knowledge_result: Optional[KnowledgeRetrievalResult] = None,
) -> List[str]:
    """
    按 query 给候选表打分。

    这里保持“便宜、稳定、可解释”的策略，不直接在这一层引入更重的 planner。
    """

    normalized_query = (user_query or "").strip().lower()
    tokens = tokenize_query(normalized_query)
    hinted_tables = resolve_query_hints(normalized_query, query_term_hints)
    knowledge_table_scores = knowledge_result.table_scores if knowledge_result else {}
    scored_tables: List[tuple[float, str]] = []

    for position, table in enumerate(metadata.tables):
        searchable_text = " ".join(
            [
                table.name,
                table.description or "",
                table.semantic_hint or "",
                table.row_grain or "",
                *[
                    " ".join(
                        filter(
                            None,
                            [
                                column.name,
                                column.data_type,
                                column.description or "",
                                column.semantic_hint or "",
                            ],
                        )
                    )
                    for column in table.columns
                ],
            ]
        ).lower()

        score = 0.0
        if table.name.lower() in normalized_query:
            score += 10.0
        if table.name in hinted_tables:
            score += 12.0
        if table.name in knowledge_table_scores:
            score += knowledge_table_scores[table.name] * 2.5

        for token in tokens:
            if len(token) <= 1:
                continue
            if token == table.name.lower():
                score += 8.0
            elif token in table.name.lower():
                score += 5.0
            if token in searchable_text:
                score += 1.5

        primary_key_match = any(pk.lower() in normalized_query for pk in table.primary_key)
        if primary_key_match:
            score += 2.0

        score += max(0.0, 0.5 - (position * 0.05))
        scored_tables.append((score, table.name))

    scored_tables.sort(key=lambda item: (-item[0], item[1]))
    return [name for score, name in scored_tables if score > 0]


def expand_tables_by_relationships(
    ranked_tables: List[str],
    relationships: List[SchemaRelationship],
    table_limit: int,
) -> List[str]:
    """在主候选表之外，沿关系补少量邻接表，支持基础多表场景。"""

    if table_limit <= 0:
        return []

    selected: List[str] = []
    seen = set()
    for table_name in ranked_tables:
        if table_name in seen:
            continue
        selected.append(table_name)
        seen.add(table_name)
        if len(selected) >= table_limit:
            return selected

    cursor = 0
    while cursor < len(selected) and len(selected) < table_limit:
        current = selected[cursor]
        for relation in relationships:
            neighbor = None
            if relation.from_table == current:
                neighbor = relation.to_table
            elif relation.to_table == current:
                neighbor = relation.from_table
            if neighbor and neighbor not in seen:
                selected.append(neighbor)
                seen.add(neighbor)
                if len(selected) >= table_limit:
                    break
        cursor += 1

    return selected


def slice_schema_metadata(
    metadata: DatabaseSchemaMetadata,
    selected_table_names: List[str],
    max_columns_per_table: int,
    prioritized_columns_by_table: Optional[Dict[str, List[str]]] = None,
) -> DatabaseSchemaMetadata:
    """把详细 schema 裁成 prompt 真正要消费的局部视图。"""

    selected_set = set(selected_table_names)
    prioritized = prioritized_columns_by_table or {}
    tables: List[TableSchema] = []
    for table in metadata.tables:
        if table.name not in selected_set:
            continue
        kept_columns = _pick_columns_for_prompt(
            table=table,
            max_columns_per_table=max_columns_per_table,
            prioritized_columns=prioritized.get(table.name, []),
        )
        tables.append(
            TableSchema(
                name=table.name,
                description=table.description,
                semantic_hint=table.semantic_hint,
                row_grain=table.row_grain,
                columns=kept_columns,
                primary_key=list(table.primary_key),
                indexes=list(table.indexes),
            )
        )

    relationships = [
        relation
        for relation in metadata.relationships
        if relation.from_table in selected_set and relation.to_table in selected_set
    ]

    notes = list(metadata.notes)
    if any(len(table.columns) > max_columns_per_table for table in metadata.tables):
        notes.append("Some tables were column-truncated for prompt budget control.")

    return DatabaseSchemaMetadata(
        source=metadata.source,
        schema_name=metadata.schema_name,
        tables=tables,
        relationships=relationships,
        notes=notes,
    )


def resolve_query_hints(
    normalized_query: str,
    query_term_hints: Dict[str, List[str]],
) -> List[str]:
    matched_tables: List[str] = []
    seen = set()
    for phrase, table_names in query_term_hints.items():
        if phrase.lower() not in normalized_query:
            continue
        for table_name in table_names:
            if table_name not in seen:
                matched_tables.append(table_name)
                seen.add(table_name)
    return matched_tables


def tokenize_query(normalized_query: str) -> List[str]:
    """保留中英文混合 query 的轻量切词逻辑，避免引入更重依赖。"""

    tokens: List[str] = []
    for chunk in re.findall(r"[\u4e00-\u9fff]+|[a-z0-9_]+", normalized_query):
        tokens.append(chunk)
        if re.fullmatch(r"[\u4e00-\u9fff]+", chunk):
            for size in (2, 3):
                if len(chunk) < size:
                    continue
                for index in range(0, len(chunk) - size + 1):
                    tokens.append(chunk[index : index + size])
    return tokens


def build_column_priority_map(
    user_query: str,
    metadata: DatabaseSchemaMetadata,
    selected_table_names: List[str],
    column_hits: List[KnowledgeHit],
) -> Dict[str, List[str]]:
    """
    构建每张表的列优先级。

    目标不是穷尽所有列，而是优先保住：
    - 主键
    - 关系列
    - query 直接命中的列
    - 字段级知识明确提示的列
    """

    normalized_query = (user_query or "").strip().lower()
    tokens = tokenize_query(normalized_query)
    selected_set = set(selected_table_names)
    relationship_columns: Dict[str, List[str]] = {}

    for relation in metadata.relationships:
        if relation.from_table in selected_set and relation.to_table in selected_set:
            relationship_columns.setdefault(relation.from_table, []).extend(relation.from_columns)
            relationship_columns.setdefault(relation.to_table, []).extend(relation.to_columns)

    hit_columns: Dict[str, List[str]] = {}
    for hit in column_hits:
        hit_columns.setdefault(hit.table_name, []).append(hit.column_name)

    priorities: Dict[str, List[str]] = {}
    for table in metadata.tables:
        if table.name not in selected_set:
            continue
        ordered: List[str] = []
        seen = set()

        def add_column(column_name: str) -> None:
            if not column_name or column_name in seen:
                return
            ordered.append(column_name)
            seen.add(column_name)

        for primary_key in table.primary_key:
            add_column(primary_key)
        for relation_column in relationship_columns.get(table.name, []):
            add_column(relation_column)
        for column_name in hit_columns.get(table.name, []):
            add_column(column_name)

        for column in table.columns:
            column_name = column.name.lower()
            semantic_text = (column.semantic_hint or "").lower()
            if column_name in normalized_query:
                add_column(column.name)
                continue
            if any(token == column_name or token in column_name for token in tokens if len(token) > 1):
                add_column(column.name)
                continue
            if semantic_text and any(token in semantic_text for token in tokens if len(token) > 1):
                add_column(column.name)

        priorities[table.name] = ordered

    return priorities


def _pick_columns_for_prompt(
    table: TableSchema,
    max_columns_per_table: int,
    prioritized_columns: List[str],
) -> List:
    columns_by_name = {column.name: column for column in table.columns}
    kept_columns = []
    kept_names = set()

    for column_name in prioritized_columns:
        column = columns_by_name.get(column_name)
        if column is None or column_name in kept_names:
            continue
        kept_columns.append(column)
        kept_names.add(column_name)
        if len(kept_columns) >= max_columns_per_table:
            return kept_columns

    for column in table.columns:
        if column.name in kept_names:
            continue
        kept_columns.append(column)
        kept_names.add(column.name)
        if len(kept_columns) >= max_columns_per_table:
            break
    return kept_columns
