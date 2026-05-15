from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


MemoryLayer = Literal["L0_RAW", "L1_SUMMARY", "L2_FACT"]
MemoryType = Literal["raw", "summary", "fact"]


class ChatRequest(BaseModel):
    """Request model for the chat endpoint."""

    session_id: str = Field(..., description="Session ID")
    query: str = Field(..., min_length=1, description="User query")
    max_retries: int = Field(2, ge=0, le=5, description="Maximum retry count; total attempts = 1 + max_retries")


class PerfettoQueryRequest(BaseModel):
    """Request model for direct Perfetto SQL debugging."""

    sql: str = Field(..., min_length=1, description="Perfetto SQL to execute")


class PerfettoAnalyzeRequest(BaseModel):
    """Request model for template-based Perfetto performance analysis."""

    problem: str = Field(..., min_length=1, description="Performance problem to analyze")
    threshold_ms: float = Field(16.0, gt=0, description="Duration threshold in milliseconds")
    limit: int = Field(20, ge=1, le=200, description="Maximum rows to inspect")


class PerfettoMetric(BaseModel):
    """Metric extracted from a Perfetto query result."""

    name: str = Field(..., description="Metric name")
    value: Any = Field(..., description="Metric value")
    unit: str = Field("", description="Metric unit")
    interpretation: str = Field("", description="Metric interpretation")


class PerfettoAnalyzeResponse(BaseModel):
    """Template-based Perfetto analysis result."""

    problem: str = Field(..., description="Analyzed problem")
    analysis_type: str = Field(..., description="Selected analysis template")
    sql: str = Field(..., description="Executed Perfetto SQL")
    metrics: List[PerfettoMetric] = Field(default_factory=list, description="Derived metrics")
    evidence: List[str] = Field(default_factory=list, description="Evidence rows summarized for review")
    conclusion: str = Field("", description="Initial conclusion")
    recommendations: List[str] = Field(default_factory=list, description="Next investigation suggestions")
    columns: List[str] = Field(default_factory=list, description="Query columns")
    rows: List[Dict[str, Any]] = Field(default_factory=list, description="Query rows")


class PerfettoAgentRequest(BaseModel):
    """Frontend-facing request model for Perfetto agent analysis."""

    session_id: str = Field(..., min_length=1, description="Session ID")
    problem: str = Field(..., min_length=1, description="Performance problem to analyze")
    dataset_id: str = Field("default-output-pb", description="Trace dataset ID")
    trace_id: Optional[str] = Field(default=None, description="Optional trace ID")
    analysis_mode: str = Field("auto", description="Analysis mode: auto/template/sql_debug")
    threshold_ms: float = Field(16.0, gt=0, description="Duration threshold in milliseconds")
    limit: int = Field(20, ge=1, le=200, description="Maximum rows to inspect")


class PerfettoReviewResult(BaseModel):
    """Rule-based review result for Perfetto agent output."""

    approved: bool = Field(..., description="Whether review passed")
    reason: str = Field(..., description="Review reason")


class PerfettoAgentResponse(BaseModel):
    """Frontend-facing response model for Perfetto agent analysis."""

    success: bool = Field(..., description="Whether analysis succeeded")
    dataset_id: str = Field(..., description="Trace dataset ID")
    trace_id: Optional[str] = Field(default=None, description="Optional trace ID")
    source_type: Literal["trace_processor", "database"] = Field(
        "trace_processor", description="Perfetto data source type"
    )
    problem: str = Field(..., description="Analyzed problem")
    analysis_type: str = Field("", description="Selected analysis template")
    plan: Dict[str, Any] = Field(default_factory=dict, description="Structured analysis plan")
    sql: str = Field("", description="Executed SQL")
    metrics: List[PerfettoMetric] = Field(default_factory=list, description="Derived metrics")
    evidence: List[str] = Field(default_factory=list, description="Evidence rows")
    conclusion: str = Field("", description="Initial conclusion")
    recommendations: List[str] = Field(default_factory=list, description="Recommendations")
    tool_calls: List[Dict[str, Any]] = Field(default_factory=list, description="Tool traces")
    review: Optional[PerfettoReviewResult] = Field(default=None, description="Review result")
    error_reason: Optional[str] = Field(default=None, description="Failure reason")


class ChartSeries(BaseModel):
    """Series payload for the frontend chart renderer."""

    name: str = Field(..., description="Series name")
    data: List[float] = Field(default_factory=list, description="Numeric series data")


class ChartConfig(BaseModel):
    """Chart payload rendered directly by the frontend."""

    chart_type: Literal["line", "bar", "pie", "table"] = Field(
        "table", description="Chart type"
    )
    title: str = Field("Analysis Result", description="Chart title")
    x_axis: List[str] = Field(default_factory=list, description="X axis labels")
    series: List[ChartSeries] = Field(default_factory=list, description="Chart series")


class ToolCallTrace(BaseModel):
    """Tool call trace compatible with future tool-use style orchestration."""

    tool_name: str = Field(..., description="Tool name")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="Tool arguments")
    success: bool = Field(..., description="Whether the call succeeded")
    output_preview: str = Field("", description="Short output preview")
    duration_ms: float = Field(0.0, ge=0.0, description="Call duration in milliseconds")


class SchemaColumn(BaseModel):
    """Structured column metadata used for prompt construction and future skills."""

    name: str = Field(..., description="Column name")
    data_type: str = Field(..., description="Database data type")
    nullable: bool = Field(True, description="Whether the column can be null")
    default_value: Optional[str] = Field(default=None, description="Default expression")
    description: Optional[str] = Field(default=None, description="Column description")
    semantic_hint: Optional[str] = Field(
        default=None, description="Business meaning or usage hint"
    )
    is_primary_key: bool = Field(False, description="Whether the column belongs to the PK")


class SchemaIndex(BaseModel):
    """Index metadata that can later guide query planning hints."""

    name: str = Field(..., description="Index name")
    columns: List[str] = Field(default_factory=list, description="Indexed columns")
    is_unique: bool = Field(False, description="Whether the index is unique")
    method: str = Field("btree", description="Index access method")
    description: Optional[str] = Field(default=None, description="Index note")


class SchemaRelationship(BaseModel):
    """Relationship metadata between tables."""

    name: str = Field(..., description="Relationship name")
    from_table: str = Field(..., description="Source table")
    from_columns: List[str] = Field(default_factory=list, description="Source columns")
    to_table: str = Field(..., description="Target table")
    to_columns: List[str] = Field(default_factory=list, description="Target columns")
    relationship_type: Literal[
        "one_to_one", "many_to_one", "one_to_many", "many_to_many"
    ] = Field("many_to_one", description="Relationship cardinality")
    inferred: bool = Field(False, description="Whether the relationship was inferred")
    description: Optional[str] = Field(default=None, description="Relationship note")


class TableSchema(BaseModel):
    """Structured table metadata."""

    name: str = Field(..., description="Table name")
    description: Optional[str] = Field(default=None, description="Table description")
    semantic_hint: Optional[str] = Field(
        default=None, description="Business purpose or semantic summary"
    )
    row_grain: Optional[str] = Field(default=None, description="Natural row grain")
    columns: List[SchemaColumn] = Field(default_factory=list, description="Columns")
    primary_key: List[str] = Field(default_factory=list, description="Primary key columns")
    indexes: List[SchemaIndex] = Field(default_factory=list, description="Indexes")


class DatabaseSchemaMetadata(BaseModel):
    """Top-level structured schema metadata."""

    source: Literal["builtin", "postgres", "perfetto"] = Field("builtin", description="Metadata source")
    schema_name: str = Field("public", description="Database schema name")
    tables: List[TableSchema] = Field(default_factory=list, description="Tables in scope")
    relationships: List[SchemaRelationship] = Field(
        default_factory=list, description="Relationships in scope"
    )
    notes: List[str] = Field(default_factory=list, description="Load notes")
    generated_at: datetime = Field(default_factory=datetime.utcnow, description="Load time")


class SchemaSelectionResult(BaseModel):
    """Progressive schema selection result for SQL generation."""

    strategy: str = Field(..., description="Schema retrieval strategy label")
    metadata_source: Literal["builtin", "postgres", "perfetto"] = Field(
        "builtin", description="Underlying metadata source"
    )
    selected_schema: DatabaseSchemaMetadata = Field(
        ..., description="Selected subset of structured schema metadata"
    )
    selected_tables: List[str] = Field(default_factory=list, description="Chosen table names")
    selected_relationships: List[str] = Field(
        default_factory=list, description="Relationship summaries kept for prompting"
    )
    prompt_text: str = Field(..., description="Prompt-ready schema text")
    prompt_budget_chars: int = Field(0, ge=0, description="Prompt text size in characters")
    fetch_mode: str = Field("single-stage", description="Schema fetch mode used before prompting")
    discovery_tables: List[str] = Field(
        default_factory=list,
        description="Candidate tables loaded in the detail-fetch stage",
    )
    query_planner_strategy: str = Field(
        "disabled",
        description="Query planner strategy used before detailed schema loading",
    )
    query_planner_reason: str = Field(
        "",
        description="Query planner summary or fallback reason",
    )
    query_planner_query_type: str = Field(
        "",
        description="Planned analysis type inferred from the user query",
    )
    query_planner_primary_metric: str = Field(
        "",
        description="Primary metric name inferred by the query planner",
    )
    query_planner_time_requirement: str = Field(
        "",
        description="Whether the query planner requires a time dimension",
    )
    query_planner_analysis_dimensions: List[str] = Field(
        default_factory=list,
        description="High-level analysis dimensions planned before field selection",
    )
    query_planner_filter_dimensions: List[str] = Field(
        default_factory=list,
        description="High-level filter dimensions planned before field selection",
    )
    query_planner_candidate_tables_hard: List[str] = Field(
        default_factory=list,
        description="Hard candidate tables required by the query planner",
    )
    query_planner_candidate_tables_soft: List[str] = Field(
        default_factory=list,
        description="Soft candidate tables suggested by the query planner",
    )
    query_planner_join_needed: bool = Field(
        False,
        description="Whether the query planner expects a join in this request",
    )
    column_selection_strategy: str = Field(
        "ordinal-truncate",
        description="Column pruning strategy used before prompting",
    )
    selected_columns_by_table: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Columns kept for each selected table after pruning",
    )
    column_planner_strategy: str = Field(
        "disabled",
        description="Column planner strategy used before column pruning",
    )
    column_planner_reason: str = Field(
        "",
        description="Column planner summary or fallback reason",
    )
    planner_required_columns_by_table: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Sanitized required columns proposed by the planner",
    )
    planner_optional_columns_by_table: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Sanitized optional columns proposed by the planner",
    )
    knowledge_strategy: str = Field("disabled", description="Knowledge retrieval strategy")
    knowledge_hit_ids: List[str] = Field(
        default_factory=list, description="Knowledge document ids used during retrieval"
    )
    knowledge_hit_titles: List[str] = Field(
        default_factory=list, description="Knowledge document titles used during retrieval"
    )
    knowledge_column_hints: List[str] = Field(
        default_factory=list,
        description="Field-level knowledge hints used during column selection",
    )
    knowledge_prompt_text: str = Field(
        "", description="Prompt-ready knowledge context text"
    )
    retrieval_notes: List[str] = Field(
        default_factory=list, description="Selection and truncation notes"
    )


class SQLExecutionResult(BaseModel):
    """Database query execution result."""

    sql: str = Field(..., description="Executed SQL")
    row_count: int = Field(0, ge=0, description="Returned row count")
    columns: List[str] = Field(default_factory=list, description="Column names")
    rows: List[Dict[str, Any]] = Field(default_factory=list, description="Returned rows")
    source: Literal["postgres", "perfetto"] = Field("postgres", description="Data source")


class WorkflowEvent(BaseModel):
    """Workflow state transition trace."""

    state: str = Field(..., description="Workflow state")
    detail: str = Field("", description="State detail")
    created_at: datetime = Field(default_factory=datetime.utcnow, description="Event time")


class AgentResponse(BaseModel):
    """Standard executor response returned to the frontend."""

    success: bool = Field(..., description="Whether execution succeeded")
    text_reply: str = Field(..., description="Natural language response")
    chart: Optional[ChartConfig] = Field(default=None, description="Optional chart payload")
    professional_findings: List[str] = Field(
        default_factory=list, description="Professional findings"
    )
    recommendations: List[str] = Field(default_factory=list, description="Recommendations")
    sql: Optional[str] = Field(default=None, description="Executed SQL")
    rows: List[Dict[str, Any]] = Field(default_factory=list, description="Query results")
    columns: List[str] = Field(default_factory=list, description="Query columns")
    tool_calls: List[ToolCallTrace] = Field(default_factory=list, description="Tool traces")
    workflow_events: List[WorkflowEvent] = Field(
        default_factory=list, description="Workflow event trace"
    )
    review_reason: Optional[str] = Field(default=None, description="Reviewer reason")
    review_debug: Optional[Dict[str, Any]] = Field(
        default=None, description="Reviewer debug payload"
    )
    error_reason: Optional[str] = Field(default=None, description="Failure reason")
    state: str = Field("DONE", description="Workflow state")
    retry_count: int = Field(0, ge=0, description="Retry count")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="Response time")


class ReviewDecision(BaseModel):
    """Reviewer decision."""

    approved: bool = Field(..., description="Whether review passed")
    reason: str = Field(..., description="Review reason")
    should_retry: bool = Field(..., description="Whether execution should retry")
    debug: Optional[Dict[str, Any]] = Field(default=None, description="Debug payload")


class SessionMemoryItem(BaseModel):
    """Session memory record."""

    id: Optional[int] = Field(default=None, description="Database primary key")
    role: str = Field(..., description="Message role")
    memory_layer: MemoryLayer = Field(..., description="Memory layer")
    memory_type: MemoryType = Field(..., description="Memory type")
    content: str = Field(..., description="Message content")
    compressed: bool = Field(False, description="Whether the item has been compressed")
    salience_score: float = Field(0.0, description="Salience score")
    source_range: Optional[List[int]] = Field(default=None, description="Summary source range")
    created_at: datetime = Field(default_factory=datetime.utcnow, description="Creation time")


class SessionMemoryResponse(BaseModel):
    """Session memory endpoint response."""

    session_id: str = Field(..., description="Session ID")
    total: int = Field(0, ge=0, description="Returned item count")
    items: List[SessionMemoryItem] = Field(default_factory=list, description="Memory items")
