from __future__ import annotations

import os
import re
from time import perf_counter
from typing import Any, Dict, Iterable, List, Optional, Tuple

from DataAnalyze.config import DB_CONFIG, EXECUTOR_LLM_CONFIG, SCHEMA_METADATA_CACHE_CONFIG
from DataAnalyze.middleware.metrics import (
    observe_column_planner,
    observe_knowledge_retrieval,
    observe_query_planner,
    observe_schema_metadata_cache,
    observe_schema_columns,
    observe_schema_fetch,
    observe_schema_metadata_load,
    observe_schema_render,
    observe_schema_selection,
)
from DataAnalyze.schemas.models import (
    DatabaseSchemaMetadata,
    SQLExecutionResult,
    SchemaColumn,
    SchemaIndex,
    SchemaRelationship,
    SchemaSelectionResult,
    TableSchema,
)
from DataAnalyze.tools.db.schema_scope import (
    PROJECT_SCHEMA_TABLE_ALLOWLIST,
    PROJECT_SCHEMA_TABLE_DENYLIST,
)
from DataAnalyze.tools.db.knowledge_retrieval import KnowledgeRetriever, KnowledgeRetrievalResult
from DataAnalyze.tools.db.field_planner import (
    build_final_column_priorities,
    build_planner_prompt_context,
    sanitize_planner_output,
)
from DataAnalyze.tools.db.query_planner import (
    build_query_plan_summary,
    build_query_planner_prompt_context,
    merge_query_planner_tables,
    sanitize_query_plan_output,
)
from DataAnalyze.tools.llm_tool import LLMClient, LLMEndpointConfig
from DataAnalyze.tools.db.postgres_schema_loader import (
    connect_postgres,
    load_schema_metadata,
    load_table_inventory,
)
from DataAnalyze.tools.db.schema_selection import (
    apply_project_table_scope,
    build_column_priority_map,
    expand_tables_by_relationships,
    rank_tables_for_query,
    resolve_query_hints,
    slice_schema_metadata,
    tokenize_query,
    to_inventory_metadata,
)
from DataAnalyze.tools.db.schema_term_hints import QUERY_TERM_HINTS


class DatabaseTool:
    """Database access tool with strict read-only execution guardrails."""

    _DANGEROUS_KEYWORDS = re.compile(
        r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE)\b",
        flags=re.IGNORECASE,
    )
    _IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    def __init__(self) -> None:
        self.db_host = os.getenv("DATAANALYZE_DB_HOST", DB_CONFIG.host)
        self.db_port = int(os.getenv("DATAANALYZE_DB_PORT", str(DB_CONFIG.port)))
        self.db_name = os.getenv("DATAANALYZE_DB_NAME", DB_CONFIG.name)
        self.db_user = os.getenv("DATAANALYZE_DB_USER", DB_CONFIG.user)
        self.db_password = os.getenv("DATAANALYZE_DB_PASSWORD", DB_CONFIG.password)
        self.db_schema = os.getenv("DATAANALYZE_DB_SCHEMA", "public")
        self.schema_scope_enabled = (
            os.getenv("DATAANALYZE_SCHEMA_SCOPE_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.schema_allowlist = self._resolve_scope_names(
            os.getenv("DATAANALYZE_SCHEMA_ALLOWLIST"),
            PROJECT_SCHEMA_TABLE_ALLOWLIST,
        )
        self.schema_denylist = self._resolve_scope_names(
            os.getenv("DATAANALYZE_SCHEMA_DENYLIST"),
            PROJECT_SCHEMA_TABLE_DENYLIST,
        )
        self.schema_metadata_cache_enabled = (
            os.getenv(
                "DATAANALYZE_SCHEMA_METADATA_CACHE_ENABLED",
                str(SCHEMA_METADATA_CACHE_CONFIG.enabled),
            )
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )
        self.schema_metadata_cache_ttl_sec = max(
            float(
                os.getenv(
                    "DATAANALYZE_SCHEMA_METADATA_CACHE_TTL_SEC",
                    str(SCHEMA_METADATA_CACHE_CONFIG.ttl_sec),
                )
            ),
            0.0,
        )
        self._schema_metadata_cache: Dict[
            Tuple[str, ...], Tuple[float, DatabaseSchemaMetadata]
        ] = {}
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

    def get_schema(self) -> str:
        """Return a prompt-friendly schema string while preserving old call sites."""

        metadata = self.get_schema_metadata()
        rendered = self.render_schema_prompt(metadata)
        observe_schema_render(mode="default")
        return rendered

    def select_schema_context(
        self,
        user_query: str,
        retry_count: int = 0,
        max_tables: Optional[int] = None,
        max_columns_per_table: Optional[int] = None,
    ) -> SchemaSelectionResult:
        """
        Progressively select a focused schema subset for SQL generation.

        Stage 2 uses heuristics first. Later phases can swap the ranking logic
        for semantic retrieval or a dedicated skill without changing callers.
        """

        # 根据是否处于重试轮次，决定本轮 schema 选择的放宽程度。
        # 首轮尽量收紧上下文；重试时再适度增加候选表和候选列预算。
        strategy = "progressive-expanded" if retry_count > 0 else "progressive-focused"
        table_limit = max_tables or (4 if retry_count > 0 else 2)
        column_limit = max_columns_per_table or (12 if retry_count > 0 else 8)

        # 先拿轻量表清单
        inventory = self.get_schema_inventory()
        fetch_mode = "two-stage"

        #宽召回，供选表使用，不区分知识类型，后续可以考虑增加针对性的召回策略和提示词设计来提升召回的相关性和多样性
        knowledge_result = self.knowledge_retriever.retrieve(
            query=user_query,
            allowed_tables=[table.name for table in inventory.tables],
        )
        observe_knowledge_retrieval(
            strategy=knowledge_result.strategy,
            outcome="success" if knowledge_result.hits else "no_hit",
            hit_count=len(knowledge_result.hits),
        )
        # 为query planner 单独准备偏“表级语义”的知识命中。
        query_planner_knowledge_result = self.knowledge_retriever.retrieve(
            query=user_query,
            allowed_tables=[table.name for table in inventory.tables],
            kinds=["table_profile", "query_pattern", "metric_definition"],
        )
        observe_knowledge_retrieval(
            strategy=query_planner_knowledge_result.strategy,
            outcome="success" if query_planner_knowledge_result.hits else "no_hit",
            hit_count=len(query_planner_knowledge_result.hits),
        )

        # 规则侧先对轻量表清单做一版粗排序，planner 失败时的保底链路。
        ranked_inventory_tables = self._rank_tables_for_query(
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
            knowledge_hits=query_planner_knowledge_result.hits,
        )
        # 第一阶段 query planner 只负责规划语义需求和 hard/soft 候选表。
        # 然后把 planner 结果与规则排序合并，得到详细 schema 加载表集合。
        detail_candidate_limit = min(
            len(inventory.tables),
            max(table_limit + 2, table_limit * 2),
        )
        detail_tables = merge_query_planner_tables(
            ranked_tables=ranked_inventory_tables,
            hard_tables=query_plan["candidate_tables_hard"],
            soft_tables=query_plan["candidate_tables_soft"],
            limit=detail_candidate_limit,
        )
        if not detail_tables:
            detail_tables = [table.name for table in inventory.tables[:detail_candidate_limit]]
        metadata = self.get_schema_metadata(detail_tables or None)

        
        ranked_tables = self._rank_tables_for_query(
            user_query=user_query,
            metadata=metadata,
            knowledge_result=knowledge_result,
        )
        
        selected_seed_tables = merge_query_planner_tables(
            ranked_tables=ranked_tables,
            hard_tables=query_plan["candidate_tables_hard"],
            soft_tables=query_plan["candidate_tables_soft"],
            limit=table_limit,
        )
        # 小范围关系扩展
        selected_tables = self._expand_tables_by_relationships(
            ranked_tables=selected_seed_tables,
            relationships=metadata.relationships,
            table_limit=table_limit,
        )
        if not selected_tables:
            selected_tables = [table.name for table in metadata.tables[:table_limit]]

      
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
        # 建立规则保底的字段优先级
        base_prioritized_columns = build_column_priority_map(
            user_query=user_query,
            metadata=metadata,
            selected_table_names=selected_tables,
            column_hits=column_hits,
        )
        query_plan_summary = build_query_plan_summary(query_plan)
        (
            column_planner_strategy,
            column_planner_reason,
            planner_required_columns_by_table,
            planner_optional_columns_by_table,
            planner_notes,
        ) = self._plan_columns_with_llm(
            user_query=user_query,
            metadata=metadata,
            selected_tables=selected_tables,
            column_hits=column_hits,
            planning_hits=planning_knowledge_result.hits,
            query_plan_summary=query_plan_summary,
        )
  
        # 如果 planner 没给出可用字段，就回退到规则版列选择。
        if planner_required_columns_by_table or planner_optional_columns_by_table:
            prioritized_columns = build_final_column_priorities(
                metadata=metadata,
                selected_tables=selected_tables,
                base_priorities=base_prioritized_columns,
                planner_required=planner_required_columns_by_table,
                planner_optional=planner_optional_columns_by_table,
            )
            column_selection_strategy = "llm-planner"
        else:
            prioritized_columns = base_prioritized_columns
            column_selection_strategy = (
                "knowledge-aware"
                if any(prioritized_columns.values())
                else "ordinal-truncate"
            )
        
        selected_schema = self._slice_schema_metadata(
            metadata=metadata,
            selected_table_names=selected_tables,
            max_columns_per_table=column_limit,
            prioritized_columns_by_table=prioritized_columns,
        )
       
        schema_prompt = self.render_schema_prompt(
            metadata=selected_schema,
            include_relationships=True,
            include_indexes=(retry_count > 0),
            max_tables=table_limit,
            max_columns_per_table=column_limit,
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
        prompt_text = schema_prompt
        if knowledge_prompt:
            prompt_text += "\n\n" + knowledge_prompt
        if column_knowledge_prompt:
            prompt_text += "\n\n" + column_knowledge_prompt
        
        observe_schema_render(mode=strategy)
        observe_schema_fetch(mode=fetch_mode, source=metadata.source)
        observe_schema_selection(
            strategy=strategy,
            source=metadata.source,
            table_count=len(selected_schema.tables),
        )
        observe_schema_columns(
            strategy=strategy,
            column_strategy=column_selection_strategy,
            column_count=sum(len(table.columns) for table in selected_schema.tables),
        )
        query_planner_table_count = len(query_plan["candidate_tables_hard"]) + len(
            query_plan["candidate_tables_soft"]
        )
        query_planner_dimension_count = len(query_plan["analysis_dimensions"]) + len(
            query_plan["filter_dimensions"]
        )
        query_planner_outcome = (
            "success"
            if query_planner_table_count > 0
            else ("disabled" if query_planner_strategy == "disabled" else "fallback")
        )
        observe_query_planner(
            strategy=query_planner_strategy,
            outcome=query_planner_outcome,
            candidate_table_count=query_planner_table_count,
            dimension_count=query_planner_dimension_count,
        )
        planner_field_count = sum(
            len(columns)
            for columns in planner_required_columns_by_table.values()
        ) + sum(len(columns) for columns in planner_optional_columns_by_table.values())
        planner_metric_outcome = (
            "success"
            if planner_field_count > 0
            else ("disabled" if column_planner_strategy == "disabled" else "fallback")
        )
        observe_column_planner(
            strategy=column_planner_strategy,
            outcome=planner_metric_outcome,
            field_count=planner_field_count,
        )

        
        relationship_summaries = [
            f"{item.from_table}({', '.join(item.from_columns)}) -> "
            f"{item.to_table}({', '.join(item.to_columns)})"
            for item in selected_schema.relationships
        ]
        selected_columns_by_table = {
            table.name: [column.name for column in table.columns]
            for table in selected_schema.tables
        }
        knowledge_column_hints = [
            f"{hit.table_name}.{hit.column_name}"
            for hit in column_hits
            if hit.table_name and hit.column_name
        ]
        retrieval_notes = list(selected_schema.notes)
        retrieval_notes.append(
            f"Selected {len(selected_schema.tables)} table(s) and "
            f"{len(selected_schema.relationships)} relationship(s) for SQL generation."
        )
        retrieval_notes.append(
            f"Column budget per table: {column_limit}; retry_count={retry_count}."
        )
        retrieval_notes.append(
            f"Query planner strategy: {query_planner_strategy}; reason={query_planner_reason}."
        )
        retrieval_notes.append(
            f"Column selection strategy: {column_selection_strategy}; column hints={len(knowledge_column_hints)}."
        )
        retrieval_notes.append(f"Query plan summary: {query_plan_summary or 'none'}.")
        retrieval_notes.append(
            f"Column planner strategy: {column_planner_strategy}; reason={column_planner_reason}."
        )
        retrieval_notes.append(
            f"Schema fetch mode: {fetch_mode}; detail tables loaded: {len(detail_tables)}."
        )
        retrieval_notes.extend(knowledge_result.notes)
        retrieval_notes.extend(query_planner_knowledge_result.notes)
        retrieval_notes.extend(column_knowledge_result.notes)
        retrieval_notes.extend(planning_knowledge_result.notes)
        retrieval_notes.extend(query_planner_notes)
        retrieval_notes.extend(planner_notes)
        return SchemaSelectionResult(
            strategy=strategy,
            metadata_source=metadata.source,
            selected_schema=selected_schema,
            selected_tables=[table.name for table in selected_schema.tables],
            selected_relationships=relationship_summaries,
            prompt_text=prompt_text,
            prompt_budget_chars=len(prompt_text),
            fetch_mode=fetch_mode,
            discovery_tables=detail_tables,
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

    def get_schema_inventory(
        self,
        table_names: Optional[Iterable[str]] = None,
    ) -> DatabaseSchemaMetadata:
        normalized_table_names = self._normalize_table_names(table_names)
        prefiltered_table_names = self._resolve_metadata_prefilter(normalized_table_names)

        inventory = self._load_table_inventory_from_postgres(prefiltered_table_names)
        if (
            inventory is None
            and prefiltered_table_names is not None
            and prefiltered_table_names != normalized_table_names
        ):
            inventory = self._load_table_inventory_from_postgres(normalized_table_names)
        if inventory is not None:
            return self._apply_project_table_scope(inventory)

        builtin_metadata = self._build_builtin_schema_metadata(normalized_table_names)
        return self._apply_project_table_scope(
            self._to_inventory_metadata(
                metadata=builtin_metadata,
                note="Builtin inventory fallback is active.",
            )
        )

    def get_schema_metadata(
        self,
        table_names: Optional[Iterable[str]] = None,
    ) -> DatabaseSchemaMetadata:
        """
        Return structured schema metadata.

        This is the stage-1 compatibility layer that future progressive retrieval
        and skill-based schema discovery can reuse directly.
        """

        normalized_table_names = self._normalize_table_names(table_names)
        cache_key = self._build_schema_cache_key(normalized_table_names)
        cached_metadata = self._get_cached_schema_metadata(cache_key)
        if cached_metadata is not None:
            observe_schema_metadata_cache("hit")
            return cached_metadata
        observe_schema_metadata_cache("miss")

        prefiltered_table_names = self._resolve_metadata_prefilter(normalized_table_names)

        postgres_start = perf_counter()
        metadata = self._load_schema_metadata_from_postgres(prefiltered_table_names)
        postgres_duration_ms = (perf_counter() - postgres_start) * 1000.0
        if (
            metadata is None
            and prefiltered_table_names is not None
            and prefiltered_table_names != normalized_table_names
        ):
            postgres_retry_start = perf_counter()
            metadata = self._load_schema_metadata_from_postgres(normalized_table_names)
            postgres_duration_ms += (perf_counter() - postgres_retry_start) * 1000.0
        if metadata is not None:
            scoped_metadata = self._apply_project_table_scope(metadata)
            observe_schema_metadata_load(
                source="postgres",
                success=True,
                duration_ms=postgres_duration_ms,
                table_count=len(scoped_metadata.tables),
            )
            self._store_schema_metadata_cache(cache_key, scoped_metadata)
            return scoped_metadata

        observe_schema_metadata_load(
            source="postgres",
            success=False,
            duration_ms=postgres_duration_ms,
            table_count=0,
        )

        builtin_start = perf_counter()
        fallback = self._apply_project_table_scope(
            self._build_builtin_schema_metadata(normalized_table_names)
        )
        builtin_duration_ms = (perf_counter() - builtin_start) * 1000.0
        observe_schema_metadata_load(
            source="builtin",
            success=True,
            duration_ms=builtin_duration_ms,
            table_count=len(fallback.tables),
        )
        self._store_schema_metadata_cache(cache_key, fallback)
        return fallback

    def render_schema_prompt(
        self,
        metadata: DatabaseSchemaMetadata,
        include_relationships: bool = True,
        include_indexes: bool = False,
        max_tables: Optional[int] = None,
        max_columns_per_table: Optional[int] = None,
    ) -> str:
        """Render structured metadata into a prompt-friendly text block."""

        tables = metadata.tables[: max_tables or len(metadata.tables)]
        sections: List[str] = []
        for table in tables:
            columns = table.columns[: max_columns_per_table or len(table.columns)]
            column_parts: List[str] = []
            for column in columns:
                flags: List[str] = []
                if column.is_primary_key:
                    flags.append("pk")
                if not column.nullable:
                    flags.append("not null")
                if column.semantic_hint:
                    flags.append(f"hint={column.semantic_hint}")
                suffix = f" [{' ; '.join(flags)}]" if flags else ""
                column_parts.append(f"{column.name} {column.data_type}{suffix}")

            table_line = f"table: {table.name}({', '.join(column_parts)})"
            table_notes: List[str] = []
            if table.semantic_hint:
                table_notes.append(f"purpose={table.semantic_hint}")
            if table.row_grain:
                table_notes.append(f"grain={table.row_grain}")
            if include_indexes and table.indexes:
                index_preview = ", ".join(
                    f"{index.name}[{','.join(index.columns)}]"
                    for index in table.indexes[:4]
                    if index.columns
                )
                if index_preview:
                    table_notes.append(f"indexes={index_preview}")
            if table_notes:
                table_line += " {" + "; ".join(table_notes) + "}"
            sections.append(table_line)

        if include_relationships and metadata.relationships:
            relationship_lines = [
                (
                    "relationship: "
                    f"{relation.from_table}({', '.join(relation.from_columns)}) -> "
                    f"{relation.to_table}({', '.join(relation.to_columns)}) "
                    f"[{relation.relationship_type}{'; inferred' if relation.inferred else ''}]"
                )
                for relation in metadata.relationships
            ]
            sections.extend(relationship_lines)

        if metadata.notes:
            sections.append("notes: " + " | ".join(metadata.notes))

        return "\n".join(sections)

    def execute_sql(self, sql: str) -> Dict[str, Any]:
        """
        Execute SQL with strict read-only checks.

        The SQL guardrails are intentionally unchanged:
        only single SELECT statements without comments are allowed.
        """

        normalized = (sql or "").strip()
        if not normalized:
            raise ValueError("SQL cannot be empty")

        if not re.match(r"^SELECT\b", normalized, flags=re.IGNORECASE):
            raise ValueError("Security guardrail: only SELECT statements are allowed")

        guardrail_error = self._scan_sql_guardrails(normalized)
        if guardrail_error:
            raise ValueError(guardrail_error)

        start = perf_counter()
        result = self._execute_postgres_query(normalized)

        payload = result.model_dump() if hasattr(result, "model_dump") else result.dict()
        payload["duration_ms"] = round((perf_counter() - start) * 1000.0, 3)
        return payload

    def _scan_sql_guardrails(self, sql: str) -> Optional[str]:
        """轻量扫描 SQL，忽略字符串字面量后再做护栏判断。"""

        masked_chars: List[str] = []
        outside_semicolons: List[int] = []
        in_single_quote = False
        in_double_quote = False
        index = 0
        length = len(sql)

        while index < length:
            char = sql[index]
            next_char = sql[index + 1] if index + 1 < length else ""

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
                index += 1
                continue

            if char == '"':
                in_double_quote = True
                masked_chars.append(" ")
                index += 1
                continue

            if char == "-" and next_char == "-":
                return "Security guardrail: SQL comments are not allowed"

            if char == "/" and next_char == "*":
                return "Security guardrail: SQL comments are not allowed"

            if char == ";":
                outside_semicolons.append(index)

            masked_chars.append(char)
            index += 1

        masked_sql = "".join(masked_chars)
        for semicolon_index in outside_semicolons:
            if masked_sql[semicolon_index + 1 :].strip():
                return "Security guardrail: multiple statements are not allowed"

        if self._DANGEROUS_KEYWORDS.search(masked_sql):
            return "Security guardrail: dangerous SQL keyword detected"

        return None

    def _execute_postgres_query(self, sql: str) -> SQLExecutionResult:
        conn = self._connect_postgres()
        if conn is None:
            raise RuntimeError(
                "PostgreSQL driver is unavailable or the connection failed; "
                "install psycopg and verify the connection settings first"
            )

        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows_raw = cur.fetchall()
                columns = [desc.name for desc in cur.description] if cur.description else []
                rows = [dict(zip(columns, row)) for row in rows_raw]
                return SQLExecutionResult(
                    sql=sql,
                    row_count=len(rows),
                    columns=columns,
                    rows=rows,
                    source="postgres",
                )
        finally:
            conn.close()

    def _connect_postgres(self) -> Optional[Any]:
        # 这里保留一个很薄的包装层，方便测试里继续 monkeypatch。
        return connect_postgres(
            host=self.db_host,
            port=self.db_port,
            db_name=self.db_name,
            user=self.db_user,
            password=self.db_password,
        )

    def _load_schema_metadata_from_postgres(
        self,
        table_names: Optional[List[str]],
    ) -> Optional[DatabaseSchemaMetadata]:
        conn = self._connect_postgres()
        if conn is None:
            return None
        # 详细 schema 的 SQL 细节已下沉到 helper，这里只保留“什么时候加载、加载什么范围”的编排语义。
        return load_schema_metadata(
            conn=conn,
            db_schema=self.db_schema,
            table_names=table_names,
            build_table_filter_clause=self._build_table_filter_clause,
            filter_table_names=self._filter_table_names,
            build_foreign_key_relationships=self._build_foreign_key_relationships,
            infer_relationships_from_tables=self._infer_relationships_from_tables,
            merge_relationships=self._merge_relationships,
        )

    def _load_table_inventory_from_postgres(
        self,
        table_names: Optional[List[str]],
    ) -> Optional[DatabaseSchemaMetadata]:
        conn = self._connect_postgres()
        if conn is None:
            return None
        # 轻量发现阶段只拿表清单，适合快速缩圈。
        return load_table_inventory(
            conn=conn,
            db_schema=self.db_schema,
            table_names=table_names,
            build_table_filter_clause=self._build_table_filter_clause,
        )

    def _build_builtin_schema_metadata(
        self,
        table_names: Optional[List[str]],
    ) -> DatabaseSchemaMetadata:
        all_tables = [
            TableSchema(
                name="chat_sessions",
                description="Conversation session registry",
                semantic_hint="One row per chat session",
                row_grain="session_id",
                primary_key=["session_id"],
                columns=[
                    SchemaColumn(
                        name="session_id",
                        data_type="varchar(128)",
                        nullable=False,
                        is_primary_key=True,
                        semantic_hint="Stable session identifier",
                    ),
                    SchemaColumn(
                        name="title",
                        data_type="varchar(255)",
                        nullable=True,
                        default_value="''",
                        semantic_hint="Optional human-readable session title",
                    ),
                    SchemaColumn(
                        name="created_at",
                        data_type="timestamptz",
                        nullable=False,
                        default_value="now()",
                        semantic_hint="Session creation time",
                    ),
                    SchemaColumn(
                        name="updated_at",
                        data_type="timestamptz",
                        nullable=False,
                        default_value="now()",
                        semantic_hint="Last session update time",
                    ),
                ],
                indexes=[],
            ),
            TableSchema(
                name="chat_memories",
                description="Layered chat memory records",
                semantic_hint="Conversation memory snapshots and facts",
                row_grain="memory record",
                primary_key=["id"],
                columns=[
                    SchemaColumn(
                        name="id",
                        data_type="bigserial",
                        nullable=False,
                        is_primary_key=True,
                        semantic_hint="Memory row identifier",
                    ),
                    SchemaColumn(
                        name="session_id",
                        data_type="varchar(128)",
                        nullable=False,
                        semantic_hint="Links the memory row to a chat session",
                    ),
                    SchemaColumn(name="role", data_type="varchar(32)", nullable=False),
                    SchemaColumn(name="memory_layer", data_type="varchar(32)", nullable=False),
                    SchemaColumn(name="memory_type", data_type="varchar(32)", nullable=False),
                    SchemaColumn(
                        name="content",
                        data_type="text",
                        nullable=False,
                        semantic_hint="Stored message or summary body",
                    ),
                    SchemaColumn(
                        name="compressed",
                        data_type="boolean",
                        nullable=False,
                        default_value="false",
                    ),
                    SchemaColumn(
                        name="salience_score",
                        data_type="numeric(6,3)",
                        nullable=True,
                        default_value="0",
                    ),
                    SchemaColumn(name="source_range_start", data_type="bigint", nullable=True),
                    SchemaColumn(name="source_range_end", data_type="bigint", nullable=True),
                    SchemaColumn(
                        name="created_at",
                        data_type="timestamptz",
                        nullable=False,
                        default_value="now()",
                    ),
                ],
                indexes=[
                    SchemaIndex(
                        name="idx_chat_memories_session_created",
                        columns=["session_id", "created_at"],
                        method="btree",
                    ),
                    SchemaIndex(
                        name="idx_chat_memories_layer",
                        columns=["session_id", "memory_layer", "created_at"],
                        method="btree",
                    ),
                ],
            ),
            TableSchema(
                name="ops_log_event",
                description="Operational log and event facts",
                semantic_hint="One row per application log or event",
                row_grain="event log row",
                primary_key=["id"],
                columns=[
                    SchemaColumn(
                        name="id",
                        data_type="bigserial",
                        nullable=False,
                        is_primary_key=True,
                        semantic_hint="Event row identifier",
                    ),
                    SchemaColumn(
                        name="ts",
                        data_type="timestamptz",
                        nullable=False,
                        semantic_hint="Observed event timestamp",
                    ),
                    SchemaColumn(
                        name="service",
                        data_type="varchar(128)",
                        nullable=False,
                        semantic_hint="Owning service or component",
                    ),
                    SchemaColumn(
                        name="host",
                        data_type="varchar(128)",
                        nullable=True,
                        semantic_hint="Infrastructure node or host",
                    ),
                    SchemaColumn(
                        name="trace_id",
                        data_type="varchar(128)",
                        nullable=True,
                        semantic_hint="Distributed trace correlation identifier",
                    ),
                    SchemaColumn(
                        name="level",
                        data_type="varchar(16)",
                        nullable=False,
                        semantic_hint="Log severity level",
                    ),
                    SchemaColumn(
                        name="error_code",
                        data_type="varchar(64)",
                        nullable=True,
                        semantic_hint="Application-specific error code",
                    ),
                    SchemaColumn(
                        name="message",
                        data_type="text",
                        nullable=False,
                        semantic_hint="Original log message body",
                    ),
                    SchemaColumn(
                        name="latency_ms",
                        data_type="numeric(12,2)",
                        nullable=True,
                        semantic_hint="Request or operation latency in milliseconds",
                    ),
                    SchemaColumn(
                        name="tags",
                        data_type="jsonb",
                        nullable=True,
                        default_value="'{}'::jsonb",
                        semantic_hint="Flexible structured attributes such as env or region",
                    ),
                    SchemaColumn(
                        name="created_at",
                        data_type="timestamptz",
                        nullable=False,
                        default_value="now()",
                    ),
                ],
                indexes=[
                    SchemaIndex(name="idx_ops_log_event_ts", columns=["ts"], method="btree"),
                    SchemaIndex(
                        name="idx_ops_log_event_service_ts",
                        columns=["service", "ts"],
                        method="btree",
                    ),
                    SchemaIndex(
                        name="idx_ops_log_event_level_ts",
                        columns=["level", "ts"],
                        method="btree",
                    ),
                    SchemaIndex(
                        name="idx_ops_log_event_error_code_ts",
                        columns=["error_code", "ts"],
                        method="btree",
                    ),
                    SchemaIndex(
                        name="idx_ops_log_event_tags_gin",
                        columns=["tags"],
                        method="gin",
                    ),
                ],
            ),
        ]

        filtered_tables = self._filter_tables(all_tables, table_names)
        relationships = self._infer_relationships_from_tables(filtered_tables)
        notes = [
            "Using builtin schema metadata fallback because PostgreSQL metadata is unavailable.",
            "This structured metadata surface is designed to be reused by future DB query skills.",
        ]
        return DatabaseSchemaMetadata(
            source="builtin",
            schema_name=self.db_schema,
            tables=filtered_tables,
            relationships=relationships,
            notes=notes,
        )

    def _build_foreign_key_relationships(
        self,
        foreign_key_rows: List[Any],
        allowed_tables: List[str],
    ) -> List[SchemaRelationship]:
        grouped: Dict[str, Dict[str, Any]] = {}
        allowed = set(allowed_tables)
        for constraint_name, table_name, column_name, foreign_table, foreign_column in foreign_key_rows:
            if table_name not in allowed or foreign_table not in allowed:
                continue
            item = grouped.setdefault(
                str(constraint_name),
                {
                    "from_table": str(table_name),
                    "from_columns": [],
                    "to_table": str(foreign_table),
                    "to_columns": [],
                },
            )
            item["from_columns"].append(str(column_name))
            item["to_columns"].append(str(foreign_column))

        relationships: List[SchemaRelationship] = []
        for constraint_name, payload in grouped.items():
            relationships.append(
                SchemaRelationship(
                    name=constraint_name,
                    from_table=payload["from_table"],
                    from_columns=payload["from_columns"],
                    to_table=payload["to_table"],
                    to_columns=payload["to_columns"],
                    relationship_type="many_to_one",
                    inferred=False,
                )
            )
        return relationships

    def _infer_relationships_from_tables(
        self,
        tables: List[TableSchema],
    ) -> List[SchemaRelationship]:
        relationships: List[SchemaRelationship] = []
        primary_key_targets: Dict[str, List[str]] = {}
        for table in tables:
            if len(table.primary_key) == 1:
                primary_key_targets.setdefault(table.primary_key[0], []).append(table.name)

        for table in tables:
            for column in table.columns:
                candidate_tables = primary_key_targets.get(column.name, [])
                if len(candidate_tables) != 1:
                    continue
                target_table = candidate_tables[0]
                if target_table == table.name:
                    continue
                relationships.append(
                    SchemaRelationship(
                        name=f"inferred_{table.name}_{column.name}_to_{target_table}",
                        from_table=table.name,
                        from_columns=[column.name],
                        to_table=target_table,
                        to_columns=[column.name],
                        relationship_type="many_to_one",
                        inferred=True,
                        description="Inferred from matching primary key column names",
                    )
                )

        return self._deduplicate_relationships(relationships)

    def _merge_relationships(
        self,
        base_relationships: List[SchemaRelationship],
        extra_relationships: List[SchemaRelationship],
    ) -> List[SchemaRelationship]:
        return self._deduplicate_relationships(base_relationships + extra_relationships)

    def _deduplicate_relationships(
        self,
        relationships: List[SchemaRelationship],
    ) -> List[SchemaRelationship]:
        deduped: List[SchemaRelationship] = []
        seen = set()
        for relation in relationships:
            key = (
                relation.from_table,
                tuple(relation.from_columns),
                relation.to_table,
                tuple(relation.to_columns),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(relation)
        return deduped

    def _normalize_table_names(
        self,
        table_names: Optional[Iterable[str]],
    ) -> Optional[List[str]]:
        if table_names is None:
            return None
        cleaned: List[str] = []
        for name in table_names:
            text = str(name or "").strip()
            if text and self._IDENTIFIER_RE.match(text):
                cleaned.append(text)
        return cleaned or None

    def _build_schema_cache_key(
        self,
        table_names: Optional[List[str]],
    ) -> Tuple[str, ...]:
        if not table_names:
            return ("__all__",)
        return tuple(sorted(set(table_names)))

    def _schema_cache_now(self) -> float:
        return perf_counter()

    def _get_cached_schema_metadata(
        self,
        cache_key: Tuple[str, ...],
    ) -> Optional[DatabaseSchemaMetadata]:
        if not self.schema_metadata_cache_enabled:
            return None
        cached_entry = self._schema_metadata_cache.get(cache_key)
        if cached_entry is None:
            return None
        cached_at, metadata = cached_entry
        if self._schema_cache_now() - cached_at > self.schema_metadata_cache_ttl_sec:
            self._schema_metadata_cache.pop(cache_key, None)
            observe_schema_metadata_cache("expired")
            return None
        return metadata

    def _store_schema_metadata_cache(
        self,
        cache_key: Tuple[str, ...],
        metadata: DatabaseSchemaMetadata,
    ) -> None:
        if not self.schema_metadata_cache_enabled:
            return
        self._schema_metadata_cache[cache_key] = (self._schema_cache_now(), metadata)
        observe_schema_metadata_cache("store")

    def _resolve_metadata_prefilter(
        self,
        requested_table_names: Optional[List[str]],
    ) -> Optional[List[str]]:
        if requested_table_names:
            return requested_table_names
        if not self.schema_scope_enabled or not self.schema_allowlist:
            return requested_table_names
        prefiltered = list(self.schema_allowlist)
        if self.schema_denylist:
            deny_set = set(self.schema_denylist)
            prefiltered = [name for name in prefiltered if name not in deny_set]
        return prefiltered or requested_table_names

    def _build_table_filter_clause(
        self,
        column_expr: str,
        table_names: Optional[List[str]],
    ) -> tuple[str, List[Any]]:
        if not table_names:
            return "", []
        return f"AND {column_expr} = ANY(%s)", [table_names]

    def _plan_query_with_llm(
        self,
        user_query: str,
        inventory: DatabaseSchemaMetadata,
        knowledge_hits: List[Any],
    ) -> tuple[str, str, Dict[str, Any], List[str]]:
        """
        在轻量表发现之后先做一次意图级 query 规划。

        第一阶段 planner 只负责：
        - 识别分析类型
        - 推断主指标和维度
        - 给出 hard/soft 候选表
        - 指出是否需要 join
        """

        default_plan: Dict[str, Any] = {
            "query_type": "detail",
            "primary_metric": "",
            "time_requirement": "optional",
            "analysis_dimensions": [],
            "filter_dimensions": [],
            "candidate_tables_hard": [],
            "candidate_tables_soft": [],
            "join_needed": False,
            "reason": "query_planner_fallback",
        }
        if not inventory.tables:
            return "fallback", "no_inventory_tables", default_plan, [
                "Query planner fallback: no inventory tables available."
            ]
        if not self.query_planner_client.is_enabled():
            return "disabled", "llm_disabled", default_plan, [
                "Query planner skipped: LLM is disabled."
            ]

        prompt_context = build_query_planner_prompt_context(
            metadata=inventory,
            knowledge_hits=knowledge_hits,
        )
        system_prompt = (
            "你是 query planner。"
            "任务是根据用户问题和轻量表清单，先规划分析意图与候选表。"
            "这里只能输出语义规划，不能输出精确字段名和 SQL。"
            "只能从给定候选表中选择 hard/soft 表。"
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
            return "fallback", f"planner_error:{type(ex).__name__}", default_plan, [
                f"Query planner fallback: {type(ex).__name__}."
            ]

        sanitized_plan, notes = sanitize_query_plan_output(
            metadata=inventory,
            raw_plan=raw_result,
        )
        candidate_table_count = len(sanitized_plan["candidate_tables_hard"]) + len(
            sanitized_plan["candidate_tables_soft"]
        )
        if candidate_table_count <= 0:
            notes.append("Query planner fallback: planner output produced no usable candidate tables.")
            return "fallback", "planner_empty", default_plan, notes

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
        """
        让 LLM 在系统边界内做字段规划。

        返回值依次为：
        - planner 策略
        - planner 原因
        - 清洗后的 required 列
        - 清洗后的 optional 列
        - 额外说明 notes
        """

        if not selected_tables:
            return "disabled", "no_selected_tables", {}, {}, ["Column planner skipped: no selected tables."]
        if not self.column_planner_client.is_enabled():
            return "disabled", "llm_disabled", {}, {}, ["Column planner skipped: LLM is disabled."]

        prompt_context = build_planner_prompt_context(
            metadata=metadata,
            selected_tables=selected_tables,
            column_hits=column_hits,
            planning_hits=planning_hits,
            query_plan_summary=query_plan_summary,
        )
        system_prompt = (
            "你是字段规划器。"
            "只能从给定 schema 中选择字段，不能臆造表和字段。"
            "目标是为本次 SQL 生成找出最小必要字段集，以及少量关键辅助字段。"
            "优先满足趋势、统计、TopN、分组、过滤、时间窗口这类分析需求。"
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

    def _to_inventory_metadata(
        self,
        metadata: DatabaseSchemaMetadata,
        note: str,
    ) -> DatabaseSchemaMetadata:
        return to_inventory_metadata(metadata=metadata, note=note)

    def _filter_table_names(
        self,
        all_table_names: List[str],
        selected_table_names: Optional[List[str]],
    ) -> List[str]:
        if not selected_table_names:
            return list(all_table_names)
        selected = set(selected_table_names)
        return [table_name for table_name in all_table_names if table_name in selected]

    def _filter_tables(
        self,
        tables: List[TableSchema],
        selected_table_names: Optional[List[str]],
    ) -> List[TableSchema]:
        if not selected_table_names:
            return tables
        selected = set(selected_table_names)
        return [table for table in tables if table.name in selected]

    def _apply_project_table_scope(
        self,
        metadata: DatabaseSchemaMetadata,
    ) -> DatabaseSchemaMetadata:
        # 这里保留薄包装，方便测试继续从 db_tool 入口验证作用域逻辑。
        return apply_project_table_scope(
            metadata=metadata,
            scope_enabled=self.schema_scope_enabled,
            allowlist=self.schema_allowlist,
            denylist=self.schema_denylist,
        )

    def _rank_tables_for_query(
        self,
        user_query: str,
        metadata: DatabaseSchemaMetadata,
        knowledge_result: Optional[KnowledgeRetrievalResult] = None,
    ) -> List[str]:
        return rank_tables_for_query(
            user_query=user_query,
            metadata=metadata,
            query_term_hints=QUERY_TERM_HINTS,
            knowledge_result=knowledge_result,
        )

    def _expand_tables_by_relationships(
        self,
        ranked_tables: List[str],
        relationships: List[SchemaRelationship],
        table_limit: int,
    ) -> List[str]:
        return expand_tables_by_relationships(
            ranked_tables=ranked_tables,
            relationships=relationships,
            table_limit=table_limit,
        )

    def _slice_schema_metadata(
        self,
        metadata: DatabaseSchemaMetadata,
        selected_table_names: List[str],
        max_columns_per_table: int,
        prioritized_columns_by_table: Optional[Dict[str, List[str]]] = None,
    ) -> DatabaseSchemaMetadata:
        return slice_schema_metadata(
            metadata=metadata,
            selected_table_names=selected_table_names,
            max_columns_per_table=max_columns_per_table,
            prioritized_columns_by_table=prioritized_columns_by_table,
        )

    def _resolve_query_hints(self, normalized_query: str) -> List[str]:
        return resolve_query_hints(
            normalized_query=normalized_query,
            query_term_hints=QUERY_TERM_HINTS,
        )

    def _tokenize_query(self, normalized_query: str) -> List[str]:
        return tokenize_query(normalized_query)

    def _resolve_scope_names(
        self,
        raw_value: Optional[str],
        default_names: List[str],
    ) -> List[str]:
        if raw_value is None:
            source_names = default_names
        else:
            source_names = [
                part.strip()
                for part in raw_value.split(",")
                if part.strip()
            ]

        cleaned: List[str] = []
        seen = set()
        for name in source_names:
            if not self._IDENTIFIER_RE.match(name):
                continue
            if name in seen:
                continue
            cleaned.append(name)
            seen.add(name)
        return cleaned
