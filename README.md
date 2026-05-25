# DataAnalyze

DataAnalyze is a FastAPI backend for LLM-assisted data and performance analysis. It provides a reusable double-agent workflow where an executor agent plans and runs read-only SQL, then a reviewer agent checks whether the result answers the user's intent.

The project currently supports two execution targets:

- PostgreSQL analysis through `DatabaseTool`
- Perfetto trace analysis through `PerfettoTool`, using the same executor/reviewer workflow whenever `analysis_mode=llm`

## Highlights

- Double-agent workflow: `ExecutorAgent -> ReviewerAgent -> retry`
- Read-only SQL guard before execution
- Progressive schema selection with query planning, column planning and knowledge retrieval
- Session memory with raw messages, summaries and extracted facts
- Perfetto `output.pb` analysis through Trace Processor SQL
- Debug pages for inspecting generated SQL, schema context, tool calls and review output
- Prometheus metrics endpoint for local observability

## Architecture

```text
POST /chat
  -> WorkflowEngine
  -> ExecutorAgent + DatabaseTool
  -> ReviewerAgent
  -> AgentResponse

POST /perfetto/agent
  -> PerfettoAgent adapter
  -> WorkflowEngine
  -> ExecutorAgent + PerfettoTool
  -> ReviewerAgent
  -> PerfettoAgentResponse
```

Important directories:

```text
agents/              Executor, reviewer and Perfetto adapter
core/                Workflow engine and session memory
knowledge/           Local YAML knowledge used by planners and retrievers
middleware/          Metrics and request monitoring
schemas/             Pydantic request/response models
tools/db/            PostgreSQL schema loading, planning and SQL execution
tools/perfetto/      Perfetto schema planning, SQL guard and Trace Processor execution
web/                 Built-in debug pages
```

## Requirements

- Python 3.10+
- PostgreSQL, if using `/chat` and persistent memory
- Ollama with `bge-m3`, optional, for vector-assisted knowledge retrieval
- Perfetto Trace Processor, if using `/perfetto/*`

Install Python dependencies:

```powershell
python -m pip install -r DataAnalyze/requirements.txt
```

If your current directory is already the `DataAnalyze` repository, run commands from its parent directory or set `PYTHONPATH` so imports like `DataAnalyze.main` can resolve.

## Configuration

Most settings can be overridden with environment variables.

Database:

```text
DATAANALYZE_DB_HOST
DATAANALYZE_DB_PORT
DATAANALYZE_DB_NAME
DATAANALYZE_DB_USER
DATAANALYZE_DB_PASSWORD
DATAANALYZE_DB_SCHEMA
DATAANALYZE_MEMORY_PERSIST
```

LLM:

```text
DATAANALYZE_LLM_API_KEY
DATAANALYZE_EXECUTOR_LLM_API_KEY
DATAANALYZE_REVIEWER_LLM_API_KEY
```

`DATAANALYZE_EXECUTOR_LLM_API_KEY` overrides the executor side only. If it is not set, the executor falls back to `DATAANALYZE_LLM_API_KEY`. Perfetto LLM mode intentionally reuses the executor configuration.

Knowledge retrieval:

```text
DATAANALYZE_KNOWLEDGE_EMBEDDING_ENABLED
DATAANALYZE_KNOWLEDGE_EMBEDDING_BASE_URL
DATAANALYZE_KNOWLEDGE_EMBEDDING_MODEL
DATAANALYZE_KNOWLEDGE_EMBEDDING_TIMEOUT_SEC
DATAANALYZE_KNOWLEDGE_RETRIEVAL_TOP_K
```

Perfetto:

```text
DATAANALYZE_PERFETTO_TRACE_PATH
DATAANALYZE_PERFETTO_TRACE_PROCESSOR_PATH
```

By default, the Perfetto path expects a local trace file named `output.pb` in the repository parent directory. Trace files are ignored by Git because they are often large and may contain sensitive runtime data.

## Run

From the parent directory of `DataAnalyze`:

```powershell
python -m uvicorn DataAnalyze.main:app --reload --host 127.0.0.1 --port 8100
```

Health check:

```powershell
curl.exe http://127.0.0.1:8100/health
```

Debug pages:

```text
http://127.0.0.1:8100/debug
http://127.0.0.1:8100/perfetto/debug
```

## API Overview

```text
GET  /health
GET  /schema
GET  /metrics
GET  /debug
GET  /perfetto/debug
GET  /sessions/{session_id}/memory
POST /chat
GET  /perfetto/schema
POST /perfetto/query
POST /perfetto/analyze
POST /perfetto/agent
```

Database analysis example:

```powershell
curl.exe -X POST http://127.0.0.1:8100/chat `
  -H "Content-Type: application/json" `
  -d "{\"session_id\":\"db-demo-1\",\"query\":\"分析最近的错误日志趋势\",\"max_retries\":2}"
```

Perfetto agent example:

```powershell
curl.exe -X POST http://127.0.0.1:8100/perfetto/agent `
  -H "Content-Type: application/json" `
  -d "{\"session_id\":\"perfetto-demo-1\",\"problem\":\"分析 CPU 占用最高的线程，并判断是否伴随长耗时 slice\",\"analysis_mode\":\"llm\",\"limit\":20}"
```

Direct Perfetto SQL example:

```powershell
curl.exe -X POST http://127.0.0.1:8100/perfetto/query `
  -H "Content-Type: application/json" `
  -d "{\"sql\":\"SELECT name, dur / 1e6 AS dur_ms FROM slice WHERE dur > 16000000 ORDER BY dur DESC LIMIT 20\"}"
```

## Debugging The Agent Chain

When debugging a full LLM run, inspect `tool_calls` in the response or open the debug page. The most useful calls are:

- `select_schema_context`: selected tables, columns, knowledge hits and planner strategy
- `generate_sql` or `generate_perfetto_sql`: LLM SQL generation details
- `validate_generated_sql`: SQL guard decision
- `execute_sql` or `execute_perfetto_sql`: execution status and row count
- `summarize_result`: LLM summary status, fallback reason and generated chart metadata
- reviewer output: approval status and reason

For Perfetto, the current intended path is:

```text
PerfettoAgent.run
  -> WorkflowEngine.process
  -> ExecutorAgent(dialect="perfetto", sql_tool=PerfettoTool)
  -> PerfettoTool.select_schema_context
  -> PerfettoTool.validate_sql
  -> PerfettoTool.execute_sql
  -> ReviewerAgent(sql_validator=PerfettoTool.validate_sql)
```

This keeps the Perfetto path close to the original DB path: only the SQL execution tool changes, while the executor, reviewer, memory and retry workflow stay reusable.


