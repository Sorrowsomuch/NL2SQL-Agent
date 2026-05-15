# DataAnalyze Technical Report for AI Handoff

## 1. Document Purpose

This report is a machine-friendly handoff package for continuing development with another AI agent.

Primary goals:
- Describe current architecture and runtime flow.
- Enumerate completed features and implementation methods.
- Define boundaries, constraints, and known risks.
- Provide actionable next-step backlog with acceptance criteria.

Version date: 2026-04-21

## 2. System Overview

Project type:
- Python backend service using FastAPI.

Core pattern:
- Hybrid orchestration model.
- Workflow state machine controls process.
- Two agents execute specialized tasks.

Main business loop:
- Receive user query.
- Build/compress context.
- Executor generates SQL and analysis.
- Reviewer validates and decides pass/retry.
- Persist memory and return structured response.

## 3. Runtime Topology

Application entry:
- DataAnalyze/main.py

Core runtime objects initialized on startup:
- MemoryManager
- DatabaseTool
- ConsoleMonitor
- ExecutorAgent
- ReviewerAgent
- WorkflowEngine

HTTP routes:
- GET /health
- GET /schema
- GET /
- GET /debug
- GET /metrics
- GET /sessions/{session_id}/memory
- POST /chat

## 4. Module Contracts

| Module | Path | Responsibility | Input | Output | Statefulness |
|---|---|---|---|---|---|
| API Layer | DataAnalyze/main.py | Expose HTTP API, glue components, handle global fallback | HTTP request models | HTTP responses (AgentResponse etc.) | Stateless per request, shared singleton objects |
| Workflow Engine | DataAnalyze/core/workflow.py | Orchestrate state transitions and retries | session_id, query, max_retries | AgentResponse with workflow events | Stateful per request attempt loop |
| Executor Agent | DataAnalyze/agents/executor.py | SQL generation, SQL execution, result analysis, chart building | user_query + context + schema | AgentResponse EXECUTOR_DONE or EXECUTOR_FAILED | Stateless per call |
| Reviewer Agent | DataAnalyze/agents/reviewer.py | Rule review + optional LLM mixed review + debug traces | AgentResponse + user_query | ReviewDecision | Stateless per call (temporary internal parse metadata) |
| Memory Manager | DataAnalyze/core/memory.py | Layered memory, compression, fact extraction, persistence | session_id and text records | prompt context, session memory views | Stateful in-memory + optional DB |
| DB Tool | DataAnalyze/tools/db_tool.py | Read-only SQL safety gate and real PostgreSQL querying | SQL string | SQLExecutionResult dict | Stateless per call |
| LLM Client | DataAnalyze/tools/llm_tool.py | OpenAI-compatible chat transport and output parsing | system_prompt + user_prompt | text/json payload | Stateless per call |
| Metrics Middleware | DataAnalyze/middleware/metrics.py | Prometheus metrics counters/histograms | method calls + labels | /metrics payload | Global metric registry |
| Console Monitor | DataAnalyze/middleware/monitor.py | Function-level duration logs | wrapped function call | console log | Stateless |
| Schemas | DataAnalyze/schemas/models.py | Pydantic contracts for request/response/state | Python objects | validated models | Stateless |

## 5. End-to-End Method Flow for POST /chat

1. main.chat(request)
2. workflow.process(session_id, query, max_retries)
3. memory_manager.add_message(user, L0_RAW)
4. loop attempt start
5. memory_manager.compress_context(session_id)
6. memory_manager.extract_facts_if_needed(session_id)
7. executor.run(...)
8. executor._run_impl(...)
9. memory_manager.build_prompt_context(...)
10. db_tool.get_schema()
11. executor._generate_sql(...)
12. executor._generate_sql_with_llm(...) or rule fallback SQL
13. db_tool.execute_sql(sql)
14. executor._analyze_result_with_llm(...) or fallback analysis
15. executor returns AgentResponse
16. reviewer.run(response, query)
17. reviewer._rule_review(...)
18. reviewer._llm_review(...) if enabled
19. reviewer parse/fallback pipeline returns ReviewDecision
20. workflow handles approved/retry/failed branch
21. memory_manager.add_message(assistant, L0_RAW) on DONE
22. main.chat returns AgentResponse

## 6. Workflow States and Decision Logic

States:
- INIT
- COMPRESS_CONTEXT
- EXECUTE
- REVIEW
- RETRY_PREPARE
- DONE
- FAILED

Retry policy:
- Loop while attempt <= retry_limit.
- Retry only when reviewer.should_retry is true.
- On each retry, last_error is fed into executor for fallback SQL path selection.

Terminal outcomes:
- DONE when review.approved is true.
- FAILED when should_retry is false or max retries exceeded.

## 7. Executor Implementation Details

## 7.1 SQL Generation

Strategy:
- First try LLM JSON output with strict SELECT-only requirement.
- If LLM unavailable/invalid, use deterministic rule SQL templates.

Current strategy label metrics:
- llm
- fallback

## 7.2 SQL Execution

Safety controls in DB tool:
- Must start with SELECT.
- Reject multiple statements.
- Reject comment injection tokens.
- Reject dangerous SQL keywords.

Execution mode:
- mock mode returns fixed synthetic rows.
- postgres mode executes real query through psycopg.

## 7.3 Result Analysis

Strategy:
- First try LLM structured JSON analysis.
- Fallback to deterministic text/findings/recommendations.

Expected LLM analysis JSON fields:
- text_reply
- professional_findings
- recommendations
- chart

Chart handling:
- Validate chart payload in _safe_chart.
- If chart invalid/table-empty, infer chart from rows.
- Fallback chart types include line/bar/pie/table.

## 8. Reviewer Implementation Details

## 8.1 Rule Review (Hard Constraints)

Checks include:
- response.success must be true.
- SQL must begin with SELECT.
- text reply must be non-empty.
- tool call trace must exist.
- analysis-like queries should include chart.
- rows must be non-empty and <= 500.

## 8.2 Mixed Review

High-level policy:
- Rule constraints are hard gate.
- LLM adds enhanced review signal.
- Weak LLM reject can fallback to rule decision.

## 8.3 LLM Parsing and Multi-layer Fault Tolerance

Parse order:
1. JSON object parse path.
2. key=value parse path.
3. natural-language heuristic parse path.
4. inference from text tokens.
5. second-pass LLM verdict normalization.
6. local verdict extraction fallback.
7. chat_json fallback if text call raises exception.

Debug fields returned in review_debug:
- mode
- llm_enabled
- parse_path
- llm_error
- raw_output_pass1
- raw_output_pass2
- raw_output_excerpt

## 9. Memory Architecture

Memory layers:
- L0_RAW: raw user/assistant/system messages.
- L1_SUMMARY: compressed structured summaries from old raw records.
- L2_FACT: reusable stable facts/constraints.

Prompt context assembly order:
- L1 summaries
- reranked L2 facts
- recent L0 raws
- current user input

Compression behavior:
- Token pressure estimated with lightweight heuristic.
- Triggered only when threshold exceeded.
- Preserves recent raw window.
- Marks source records compressed and stores summary with source_range.

Persistence:
- Optional via DATAANALYZE_MEMORY_PERSIST.
- Reads/writes chat_sessions and chat_memories when DB available.
- Falls back to in-memory mode on DB issues.

## 10. API Contracts

POST /chat request:
- session_id: string
- query: string
- max_retries: int (0..5)

POST /chat response key fields:
- success
- text_reply
- chart
- professional_findings
- recommendations
- sql
- rows
- columns
- tool_calls
- workflow_events
- review_reason
- review_debug
- error_reason
- state
- retry_count
- timestamp

GET /sessions/{session_id}/memory:
- supports filters layer and include_compressed.
- returns memory records with layer/type/meta.

GET /metrics:
- Prometheus scrape endpoint.

## 11. Monitoring Implementation

Metrics endpoint:
- /metrics in main.py

HTTP metrics:
- dataanalyze_http_requests_total
- dataanalyze_http_request_duration_seconds

Executor chain metrics:
- dataanalyze_executor_runs_total
- dataanalyze_executor_sql_generation_total
- dataanalyze_executor_analysis_total
- dataanalyze_executor_chart_type_total
- dataanalyze_executor_rows_returned

DB metrics:
- dataanalyze_db_query_duration_seconds

Stack:
- Prometheus and Grafana via docker-compose.monitoring.yml
- Auto-provisioned datasource and dashboard.

## 12. Data Assets and SQL Files

Primary SQL artifacts:
- DataAnalyze/sql/dataanalyze_schema.sql
- DataAnalyze/sql/ops_log_event_sample_data.sql
- DataAnalyze/sql/ops_log_event_diverse_seed.sql

Purpose:
- Create core tables.
- Provide quick sample data.
- Provide diverse stress data (service mix, incidents, slow query, edge cases).

## 13. Security and Reliability Boundaries

Current protections:
- Read-only SQL enforcement.
- Multi-layer review fallback.
- Error fallback AgentResponse at API boundary.
- Sensitive text sanitization in reviewer debug excerpts.

Current gaps:
- config.py still contains plaintext secrets and DB credentials.
- No API auth for chat/debug endpoints.
- No role-based access controls.
- Limited automated tests for parser and workflow reliability.

## 14. Known Technical Debt

1. Schema understanding is shallow for large multi-table domains.
2. LLM output formatting still model-dependent.
3. Memory summarization is rule-based, not semantic LLM summarization.
4. SQL generation fallback templates are limited.
5. Monitoring is minimal and lacks alert rules.
6. README config examples may diverge from current operational defaults over time.

## 15. Completed Work Summary

Completed architecture:
- FastAPI service with stateful workflow orchestration.
- Executor + Reviewer dual-agent pattern.
- Layered memory with persistence and compression.
- Read-only DB execution guardrails.
- LLM integration for SQL generation, analysis, and review.
- Rich review_debug diagnostics.
- Built-in debug web page.
- Prometheus + Grafana minimal chain observability.

Completed reliability improvements:
- Reviewer parse pipeline hardened for JSON/KV/NL/infer paths.
- Second-pass normalization and local extraction fallback.
- Weak reject fallback policy to reduce false negatives.
- Chart inference to avoid unnecessary table-only visualization.

## 16. AI Handoff Instructions for Next Phase

Hard invariants to preserve:
- Never allow non-SELECT SQL execution.
- Keep workflow states and retry semantics unchanged unless explicitly redesigning workflow.
- Preserve AgentResponse schema compatibility for frontend/debug tools.
- Preserve review_debug payload fields for observability.

Recommended next-phase roadmap:
1. Replace plaintext secrets with env-based secret injection.
2. Add auth and rate limiting to API.
3. Add automated tests for reviewer parse paths and retry logic.
4. Add alerting rules for fallback-rate spikes and DB latency.
5. Expand SQL planner for multi-table real schema.
6. Optionally add LLM-based memory summarization with deterministic fallback.

Acceptance criteria template for next tasks:
- Functional criteria: exact behavior change expected.
- Safety criteria: SQL guardrails and fallback behavior remain intact.
- Observability criteria: metrics/debug fields updated.
- Regression criteria: workflow and response contracts unchanged.

## 17. Suggested Prompt to Continue with Another AI

Use this prompt template with the next AI:

"Read DataAnalyze/technical_report_for_ai_handoff_2026-04-21.md first.
Then implement <TASK_NAME> under these constraints:
1) Keep SQL read-only guardrails unchanged.
2) Keep /chat response schema backward compatible.
3) Add or update metrics and review_debug when behavior changes.
4) Provide compile/test evidence and changed-file summary."


复杂sql
生产环境多表查询
字段-含义映射
