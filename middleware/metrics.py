from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

HTTP_REQUEST_TOTAL = Counter(
    "dataanalyze_http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "dataanalyze_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path"],
    buckets=(0.01, 0.03, 0.05, 0.1, 0.2, 0.5, 1, 2, 5),
)

EXECUTOR_RUN_TOTAL = Counter(
    "dataanalyze_executor_runs_total",
    "Total executor runs",
    ["status"],
)

EXECUTOR_SQL_GENERATION_TOTAL = Counter(
    "dataanalyze_executor_sql_generation_total",
    "Executor SQL generation strategy count",
    ["strategy"],
)

EXECUTOR_SQL_GUARD_TOTAL = Counter(
    "dataanalyze_executor_sql_guard_total",
    "Executor SQL generation guard outcome count",
    ["outcome", "reason"],
)

EXECUTOR_ANALYSIS_TOTAL = Counter(
    "dataanalyze_executor_analysis_total",
    "Executor analysis strategy count",
    ["strategy"],
)

EXECUTOR_CHART_TYPE_TOTAL = Counter(
    "dataanalyze_executor_chart_type_total",
    "Executor output chart type count",
    ["chart_type"],
)

EXECUTOR_ROWS_RETURNED = Histogram(
    "dataanalyze_executor_rows_returned",
    "Rows returned by executor SQL",
    buckets=(0, 1, 5, 10, 20, 50, 100, 200, 500, 1000),
)

DB_QUERY_DURATION_SECONDS = Histogram(
    "dataanalyze_db_query_duration_seconds",
    "DB query duration in seconds",
    ["source", "success"],
    buckets=(0.005, 0.01, 0.03, 0.05, 0.1, 0.2, 0.5, 1, 2, 5),
)

SCHEMA_METADATA_LOAD_TOTAL = Counter(
    "dataanalyze_schema_metadata_load_total",
    "Structured schema metadata load attempts",
    ["source", "success"],
)

SCHEMA_METADATA_LOAD_DURATION_SECONDS = Histogram(
    "dataanalyze_schema_metadata_load_duration_seconds",
    "Structured schema metadata load duration in seconds",
    ["source", "success"],
    buckets=(0.001, 0.005, 0.01, 0.03, 0.05, 0.1, 0.2, 0.5, 1, 2, 5),
)

SCHEMA_TABLES_RETURNED = Histogram(
    "dataanalyze_schema_tables_returned",
    "Number of schema tables returned per load",
    ["source"],
    buckets=(0, 1, 3, 5, 10, 20, 50, 100, 200),
)

SCHEMA_METADATA_CACHE_TOTAL = Counter(
    "dataanalyze_schema_metadata_cache_total",
    "Schema metadata cache access count",
    ["outcome"],
)

SCHEMA_RENDER_TOTAL = Counter(
    "dataanalyze_schema_render_total",
    "Schema prompt render count",
    ["mode"],
)

SCHEMA_SELECTION_TOTAL = Counter(
    "dataanalyze_schema_selection_total",
    "Progressive schema selection count",
    ["strategy", "source"],
)

SCHEMA_FETCH_TOTAL = Counter(
    "dataanalyze_schema_fetch_total",
    "Schema fetch mode count",
    ["mode", "source"],
)

SCHEMA_SELECTED_TABLES = Histogram(
    "dataanalyze_schema_selected_tables",
    "Number of selected tables used for SQL generation",
    ["strategy"],
    buckets=(0, 1, 2, 3, 4, 6, 8, 12, 20),
)

SCHEMA_SELECTED_COLUMNS = Histogram(
    "dataanalyze_schema_selected_columns",
    "Number of selected columns kept in prompt after column pruning",
    ["strategy", "column_strategy"],
    buckets=(0, 2, 4, 8, 12, 16, 24, 32, 48, 64),
)

COLUMN_PLANNER_TOTAL = Counter(
    "dataanalyze_column_planner_total",
    "Column planner attempts grouped by strategy and outcome",
    ["strategy", "outcome"],
)

COLUMN_PLANNER_FIELDS = Histogram(
    "dataanalyze_column_planner_fields",
    "Number of planner-proposed columns after sanitization",
    ["strategy", "outcome"],
    buckets=(0, 1, 2, 4, 8, 12, 16, 24, 32),
)

QUERY_PLANNER_TOTAL = Counter(
    "dataanalyze_query_planner_total",
    "Query planner attempts grouped by strategy and outcome",
    ["strategy", "outcome"],
)

QUERY_PLANNER_CANDIDATE_TABLES = Histogram(
    "dataanalyze_query_planner_candidate_tables",
    "Number of candidate tables proposed by the query planner",
    ["strategy", "outcome"],
    buckets=(0, 1, 2, 3, 4, 6, 8, 12, 20),
)

QUERY_PLANNER_DIMENSIONS = Histogram(
    "dataanalyze_query_planner_dimensions",
    "Number of analysis and filter dimensions proposed by the query planner",
    ["strategy", "outcome"],
    buckets=(0, 1, 2, 3, 4, 6, 8, 12),
)

KNOWLEDGE_RETRIEVAL_TOTAL = Counter(
    "dataanalyze_knowledge_retrieval_total",
    "Knowledge retrieval attempts grouped by strategy and outcome",
    ["strategy", "outcome"],
)

KNOWLEDGE_RETRIEVAL_HITS = Histogram(
    "dataanalyze_knowledge_retrieval_hits",
    "Number of knowledge hits returned per retrieval",
    ["strategy"],
    buckets=(0, 1, 2, 3, 4, 6, 8, 12, 20),
)

MEMORY_COMPRESSION_TOTAL = Counter(
    "dataanalyze_memory_compression_total",
    "Memory compression attempts grouped by strategy and outcome",
    ["strategy", "outcome"],
)

MEMORY_FACT_EXTRACTION_TOTAL = Counter(
    "dataanalyze_memory_fact_extraction_total",
    "Memory fact extraction attempts grouped by strategy and outcome",
    ["strategy", "outcome"],
)

MEMORY_FACT_COUNT = Histogram(
    "dataanalyze_memory_fact_count",
    "Number of facts produced in each memory extraction attempt",
    ["strategy", "outcome"],
    buckets=(0, 1, 2, 3, 5, 8, 12, 20),
)


def observe_http_request(method: str, path: str, status: int, duration_sec: float) -> None:
    m = (method or "UNKNOWN").upper()
    p = path or "unknown"
    s = str(status)
    HTTP_REQUEST_TOTAL.labels(method=m, path=p, status=s).inc()
    HTTP_REQUEST_DURATION_SECONDS.labels(method=m, path=p).observe(max(duration_sec, 0.0))


def observe_executor_sql_generation(strategy: str) -> None:
    EXECUTOR_SQL_GENERATION_TOTAL.labels(strategy=strategy or "unknown").inc()


def observe_executor_sql_guard(outcome: str, reason: str) -> None:
    EXECUTOR_SQL_GUARD_TOTAL.labels(
        outcome=outcome or "unknown",
        reason=reason or "unknown",
    ).inc()


def observe_executor_analysis_strategy(strategy: str) -> None:
    EXECUTOR_ANALYSIS_TOTAL.labels(strategy=strategy or "unknown").inc()


def observe_executor_result(success: bool, rows: int, chart_type: str) -> None:
    EXECUTOR_RUN_TOTAL.labels(status="success" if success else "error").inc()
    EXECUTOR_ROWS_RETURNED.observe(max(rows, 0))
    EXECUTOR_CHART_TYPE_TOTAL.labels(chart_type=chart_type or "none").inc()


def observe_db_query(source: str, success: bool, duration_ms: float) -> None:
    sec = max(duration_ms, 0.0) / 1000.0
    DB_QUERY_DURATION_SECONDS.labels(
        source=source or "unknown", success="true" if success else "false"
    ).observe(sec)


def observe_schema_metadata_load(
    source: str, success: bool, duration_ms: float, table_count: int
) -> None:
    normalized_source = source or "unknown"
    success_label = "true" if success else "false"
    SCHEMA_METADATA_LOAD_TOTAL.labels(
        source=normalized_source, success=success_label
    ).inc()
    SCHEMA_METADATA_LOAD_DURATION_SECONDS.labels(
        source=normalized_source, success=success_label
    ).observe(max(duration_ms, 0.0) / 1000.0)
    SCHEMA_TABLES_RETURNED.labels(source=normalized_source).observe(max(table_count, 0))


def observe_schema_metadata_cache(outcome: str) -> None:
    SCHEMA_METADATA_CACHE_TOTAL.labels(outcome=outcome or "unknown").inc()


def observe_schema_render(mode: str) -> None:
    SCHEMA_RENDER_TOTAL.labels(mode=mode or "unknown").inc()


def observe_schema_selection(strategy: str, source: str, table_count: int) -> None:
    normalized_strategy = strategy or "unknown"
    normalized_source = source or "unknown"
    SCHEMA_SELECTION_TOTAL.labels(
        strategy=normalized_strategy, source=normalized_source
    ).inc()
    SCHEMA_SELECTED_TABLES.labels(strategy=normalized_strategy).observe(max(table_count, 0))


def observe_schema_fetch(mode: str, source: str) -> None:
    SCHEMA_FETCH_TOTAL.labels(
        mode=mode or "unknown",
        source=source or "unknown",
    ).inc()


def observe_schema_columns(strategy: str, column_strategy: str, column_count: int) -> None:
    SCHEMA_SELECTED_COLUMNS.labels(
        strategy=strategy or "unknown",
        column_strategy=column_strategy or "unknown",
    ).observe(max(column_count, 0))


def observe_column_planner(strategy: str, outcome: str, field_count: int) -> None:
    normalized_strategy = strategy or "unknown"
    normalized_outcome = outcome or "unknown"
    COLUMN_PLANNER_TOTAL.labels(
        strategy=normalized_strategy,
        outcome=normalized_outcome,
    ).inc()
    COLUMN_PLANNER_FIELDS.labels(
        strategy=normalized_strategy,
        outcome=normalized_outcome,
    ).observe(max(field_count, 0))


def observe_query_planner(
    strategy: str,
    outcome: str,
    candidate_table_count: int,
    dimension_count: int,
) -> None:
    normalized_strategy = strategy or "unknown"
    normalized_outcome = outcome or "unknown"
    QUERY_PLANNER_TOTAL.labels(
        strategy=normalized_strategy,
        outcome=normalized_outcome,
    ).inc()
    QUERY_PLANNER_CANDIDATE_TABLES.labels(
        strategy=normalized_strategy,
        outcome=normalized_outcome,
    ).observe(max(candidate_table_count, 0))
    QUERY_PLANNER_DIMENSIONS.labels(
        strategy=normalized_strategy,
        outcome=normalized_outcome,
    ).observe(max(dimension_count, 0))


def observe_knowledge_retrieval(strategy: str, outcome: str, hit_count: int) -> None:
    normalized_strategy = strategy or "unknown"
    normalized_outcome = outcome or "unknown"
    KNOWLEDGE_RETRIEVAL_TOTAL.labels(
        strategy=normalized_strategy,
        outcome=normalized_outcome,
    ).inc()
    KNOWLEDGE_RETRIEVAL_HITS.labels(strategy=normalized_strategy).observe(max(hit_count, 0))


def observe_memory_compression(strategy: str, outcome: str) -> None:
    MEMORY_COMPRESSION_TOTAL.labels(
        strategy=strategy or "unknown",
        outcome=outcome or "unknown",
    ).inc()


def observe_memory_fact_extraction(strategy: str, outcome: str, count: int) -> None:
    normalized_strategy = strategy or "unknown"
    normalized_outcome = outcome or "unknown"
    MEMORY_FACT_EXTRACTION_TOTAL.labels(
        strategy=normalized_strategy,
        outcome=normalized_outcome,
    ).inc()
    MEMORY_FACT_COUNT.labels(
        strategy=normalized_strategy,
        outcome=normalized_outcome,
    ).observe(max(count, 0))


def build_metrics_payload() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
