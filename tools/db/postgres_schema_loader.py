from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from DataAnalyze.schemas.models import (
    DatabaseSchemaMetadata,
    SchemaColumn,
    SchemaIndex,
    SchemaRelationship,
    TableSchema,
)


def connect_postgres(
    host: str,
    port: int,
    db_name: str,
    user: str,
    password: str,
) -> Optional[Any]:
    """建立 PostgreSQL 连接。失败时返回 None，交由上层决定是否 fallback。"""

    try:
        import psycopg
    except Exception:
        return None

    try:
        return psycopg.connect(
            host=host,
            port=port,
            dbname=db_name,
            user=user,
            password=password,
        )
    except Exception:
        return None


def load_table_inventory(
    conn: Any,
    db_schema: str,
    table_names: Optional[List[str]],
    build_table_filter_clause: Callable[[str, Optional[List[str]]], tuple[str, List[Any]]],
) -> Optional[DatabaseSchemaMetadata]:
    """
    只加载轻量表清单。

    这一层故意只拿表名，不拿列、索引、外键，便于在 schema 选择前先做便宜的候选发现。
    """

    try:
        with conn.cursor() as cur:
            table_filter_sql, table_filter_params = build_table_filter_clause(
                column_expr="table_name",
                table_names=table_names,
            )
            cur.execute(
                f"""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = %s
                  AND table_type IN ('BASE TABLE', 'VIEW')
                  {table_filter_sql}
                ORDER BY table_name
                """,
                (db_schema, *table_filter_params),
            )
            table_rows = [str(row[0]) for row in cur.fetchall()]
    except Exception:
        return None
    finally:
        conn.close()

    if not table_rows:
        return None

    return DatabaseSchemaMetadata(
        source="postgres",
        schema_name=db_schema,
        tables=[TableSchema(name=table_name) for table_name in table_rows],
        notes=["Lightweight table inventory is ready for candidate discovery."],
    )


def load_schema_metadata(
    conn: Any,
    db_schema: str,
    table_names: Optional[List[str]],
    build_table_filter_clause: Callable[[str, Optional[List[str]]], tuple[str, List[Any]]],
    filter_table_names: Callable[[List[str], Optional[List[str]]], List[str]],
    build_foreign_key_relationships: Callable[
        [List[Tuple[Any, ...]], List[str]], List[SchemaRelationship]
    ],
    infer_relationships_from_tables: Callable[[List[TableSchema]], List[SchemaRelationship]],
    merge_relationships: Callable[
        [List[SchemaRelationship], List[SchemaRelationship]], List[SchemaRelationship]
    ],
) -> Optional[DatabaseSchemaMetadata]:
    """
    加载详细 schema 元数据。

    这里集中处理 PostgreSQL catalog 查询和结构化装配，避免 db_tool.py 既负责编排又负责大量 SQL 细节。
    """

    try:
        with conn.cursor() as cur:
            table_filter_sql, table_filter_params = build_table_filter_clause(
                column_expr="table_name",
                table_names=table_names,
            )
            primary_key_filter_sql, primary_key_filter_params = build_table_filter_clause(
                column_expr="tc.table_name",
                table_names=table_names,
            )
            index_filter_sql, index_filter_params = build_table_filter_clause(
                column_expr="tab.relname",
                table_names=table_names,
            )

            cur.execute(
                f"""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = %s
                  AND table_type IN ('BASE TABLE', 'VIEW')
                  {table_filter_sql}
                ORDER BY table_name
                """,
                (db_schema, *table_filter_params),
            )
            table_rows = [str(row[0]) for row in cur.fetchall()]

            cur.execute(
                f"""
                SELECT
                    table_name,
                    column_name,
                    data_type,
                    is_nullable,
                    column_default,
                    ordinal_position
                FROM information_schema.columns
                WHERE table_schema = %s
                  {table_filter_sql}
                ORDER BY table_name, ordinal_position
                """,
                (db_schema, *table_filter_params),
            )
            column_rows = cur.fetchall()

            cur.execute(
                f"""
                SELECT
                    tc.table_name,
                    kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                WHERE tc.table_schema = %s
                  AND tc.constraint_type = 'PRIMARY KEY'
                  {primary_key_filter_sql}
                ORDER BY tc.table_name, kcu.ordinal_position
                """,
                (db_schema, *primary_key_filter_params),
            )
            primary_key_rows = cur.fetchall()

            foreign_key_filter_sql = ""
            foreign_key_filter_params: List[Any] = []
            if table_names:
                foreign_key_filter_sql = "AND (kcu.table_name = ANY(%s) OR ccu.table_name = ANY(%s))"
                foreign_key_filter_params = [table_names, table_names]
            cur.execute(
                f"""
                SELECT
                    tc.constraint_name,
                    kcu.table_name,
                    kcu.column_name,
                    ccu.table_name AS foreign_table_name,
                    ccu.column_name AS foreign_column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                JOIN information_schema.constraint_column_usage ccu
                  ON tc.constraint_name = ccu.constraint_name
                 AND tc.table_schema = ccu.table_schema
                WHERE tc.table_schema = %s
                  AND tc.constraint_type = 'FOREIGN KEY'
                  {foreign_key_filter_sql}
                ORDER BY tc.constraint_name, kcu.ordinal_position
                """,
                (db_schema, *foreign_key_filter_params),
            )
            foreign_key_rows = cur.fetchall()

            cur.execute(
                f"""
                SELECT
                    tab.relname AS table_name,
                    idx.relname AS index_name,
                    ind.indisunique AS is_unique,
                    am.amname AS access_method,
                    array_agg(att.attname ORDER BY arr.ord) AS column_names
                FROM pg_class tab
                JOIN pg_namespace ns
                  ON ns.oid = tab.relnamespace
                JOIN pg_index ind
                  ON tab.oid = ind.indrelid
                JOIN pg_class idx
                  ON idx.oid = ind.indexrelid
                JOIN pg_am am
                  ON idx.relam = am.oid
                JOIN LATERAL unnest(ind.indkey) WITH ORDINALITY arr(attnum, ord)
                  ON TRUE
                LEFT JOIN pg_attribute att
                  ON att.attrelid = tab.oid
                 AND att.attnum = arr.attnum
                WHERE ns.nspname = %s
                  {index_filter_sql}
                GROUP BY tab.relname, idx.relname, ind.indisunique, am.amname
                ORDER BY tab.relname, idx.relname
                """,
                (db_schema, *index_filter_params),
            )
            index_rows = cur.fetchall()
    except Exception:
        return None
    finally:
        conn.close()

    filtered_table_names = filter_table_names(table_rows, table_names)
    if not filtered_table_names:
        return None

    primary_key_map = _build_primary_key_map(primary_key_rows, filtered_table_names)
    index_map = _build_index_map(index_rows, filtered_table_names)
    column_map = _build_column_map(column_rows, filtered_table_names, primary_key_map)
    tables = _build_tables(filtered_table_names, column_map, primary_key_map, index_map)

    relationships = build_foreign_key_relationships(
        foreign_key_rows=foreign_key_rows,
        allowed_tables=filtered_table_names,
    )
    relationships = merge_relationships(
        relationships,
        infer_relationships_from_tables(tables),
    )

    return DatabaseSchemaMetadata(
        source="postgres",
        schema_name=db_schema,
        tables=tables,
        relationships=relationships,
        notes=["Structured schema metadata is ready for future progressive retrieval and skill wrapping."],
    )


def _build_primary_key_map(
    primary_key_rows: List[Tuple[Any, ...]],
    filtered_table_names: List[str],
) -> Dict[str, List[str]]:
    primary_key_map: Dict[str, List[str]] = {}
    for table_name, column_name in primary_key_rows:
        if table_name in filtered_table_names:
            primary_key_map.setdefault(str(table_name), []).append(str(column_name))
    return primary_key_map


def _build_index_map(
    index_rows: List[Tuple[Any, ...]],
    filtered_table_names: List[str],
) -> Dict[str, List[SchemaIndex]]:
    index_map: Dict[str, List[SchemaIndex]] = {}
    for table_name, index_name, is_unique, method, column_names in index_rows:
        if table_name not in filtered_table_names:
            continue
        clean_columns = [str(name) for name in (column_names or []) if name]
        index_map.setdefault(str(table_name), []).append(
            SchemaIndex(
                name=str(index_name),
                columns=clean_columns,
                is_unique=bool(is_unique),
                method=str(method or "btree"),
            )
        )
    return index_map


def _build_column_map(
    column_rows: List[Tuple[Any, ...]],
    filtered_table_names: List[str],
    primary_key_map: Dict[str, List[str]],
) -> Dict[str, List[SchemaColumn]]:
    column_map: Dict[str, List[SchemaColumn]] = {}
    for table_name, column_name, data_type, is_nullable, default_value, _ in column_rows:
        if table_name not in filtered_table_names:
            continue
        normalized_table = str(table_name)
        normalized_column = str(column_name)
        column_map.setdefault(normalized_table, []).append(
            SchemaColumn(
                name=normalized_column,
                data_type=str(data_type),
                nullable=str(is_nullable).upper() == "YES",
                default_value=None if default_value is None else str(default_value),
                is_primary_key=normalized_column in primary_key_map.get(normalized_table, []),
            )
        )
    return column_map


def _build_tables(
    filtered_table_names: List[str],
    column_map: Dict[str, List[SchemaColumn]],
    primary_key_map: Dict[str, List[str]],
    index_map: Dict[str, List[SchemaIndex]],
) -> List[TableSchema]:
    return [
        TableSchema(
            name=table_name,
            columns=column_map.get(table_name, []),
            primary_key=primary_key_map.get(table_name, []),
            indexes=index_map.get(table_name, []),
        )
        for table_name in filtered_table_names
    ]
