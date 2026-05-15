# DataAnalyze 后端说明

## Perfetto LLM 调试速查

当前 Perfetto 入口：

- 前端调试页：`GET /perfetto/debug`
- Agent 接口：`POST /perfetto/agent`
- 直接 SQL 调试：`POST /perfetto/query`
- Perfetto schema 查看：`GET /perfetto/schema`

### 运行服务

```powershell
python -m uvicorn DataAnalyze.main:app --reload --host 127.0.0.1 --port 8100
```

打开：

```text
http://127.0.0.1:8100/perfetto/debug
```

### Perfetto LLM 配置

Perfetto LLM 使用独立配置，不影响 `/chat` 的 executor/reviewer：

```powershell
$env:DATAANALYZE_PERFETTO_LLM_ENABLED="true"
$env:DATAANALYZE_PERFETTO_LLM_BASE_URL="https://api.deepseek.com/v1"
$env:DATAANALYZE_PERFETTO_LLM_API_KEY="your-key"
$env:DATAANALYZE_PERFETTO_LLM_MODEL="deepseek-chat"
$env:DATAANALYZE_PERFETTO_LLM_TIMEOUT_SEC="60"
```

如果使用 Ollama 的 OpenAI-compatible 接口：

```powershell
$env:DATAANALYZE_PERFETTO_LLM_ENABLED="true"
$env:DATAANALYZE_PERFETTO_LLM_BASE_URL="http://127.0.0.1:11434/v1"
$env:DATAANALYZE_PERFETTO_LLM_API_KEY="ollama"
$env:DATAANALYZE_PERFETTO_LLM_MODEL="qwen2.5:7b"
```

配置后需要重启 uvicorn。未配置 key 或未设置 `DATAANALYZE_PERFETTO_LLM_ENABLED=true` 时，`analysis_mode=llm` 会返回可读错误。

### 调试页面怎么看

`/perfetto/debug` 的 `analysis_mode`：

- `template`：走当前模板链路，不调用 LLM。
- `llm`：强制走 `PerfettoExecutorAgent`，由 LLM 生成 Perfetto SQL，再执行和汇总。
- `auto`：当前仍走模板链路，后续 Step 3 会改成 LLM 优先、失败 fallback 到模板。

响应页签：

- `概览`：看 success、review、metrics、evidence、conclusion。
- `SQL`：看最终执行的 Perfetto SQL。
- `Tool Calls`：看每一步调试信息，这是排查 LLM 是否正常工作的重点。
- `Raw JSON`：看完整接口响应。

LLM 正常工作时，`Tool Calls` 里应至少看到：

- `select_perfetto_schema_context`：本次给 LLM 的 Perfetto schema prompt。
- `generate_perfetto_sql`：SQL 生成步骤。
  - `arguments.prompt_debug`：发给 LLM 的问题、阈值、limit、schema。
  - `arguments.llm_response`：LLM 返回的解析后 JSON，包含 `sql / analysis_type / reason`。
- `validate_perfetto_sql`：SQL guard 结果。
- `execute_perfetto_sql`：trace processor 执行结果概览。
- `summarize_perfetto_result`：结果汇总步骤。
  - `arguments.rows_sample`：发给 LLM 的查询结果样本。
  - `arguments.llm_response`：LLM 返回的指标、证据、结论、建议。
  - `arguments.normalized_summary`：系统规范化后的最终输出。

### 最小请求示例

```powershell
curl.exe -X POST http://127.0.0.1:8100/perfetto/agent `
  -H "Content-Type: application/json" `
  -d "{\"session_id\":\"s1\",\"problem\":\"分析主线程卡顿\",\"analysis_mode\":\"llm\",\"limit\":20}"
```

如果只想确认非 LLM 链路：

```powershell
curl.exe -X POST http://127.0.0.1:8100/perfetto/agent `
  -H "Content-Type: application/json" `
  -d "{\"session_id\":\"s1\",\"problem\":\"分析主线程卡顿\",\"analysis_mode\":\"template\",\"limit\":20}"
```

## 当前能力

- FastAPI 后端服务，提供 `/chat`、`/health`、`/schema`、`/debug`、`/metrics`、`/sessions/{session_id}/memory`
- 双 Agent 主链路
  - `Executor` 负责 schema 选择、SQL 生成、SQL 执行、结果分析
  - `Reviewer` 负责规则评审和可选 LLM 复核
- 工作流状态机
  - `INIT -> COMPRESS_CONTEXT -> EXECUTE -> REVIEW -> RETRY_PREPARE -> DONE/FAILED`
- 分层记忆系统
  - `L0_RAW / L1_SUMMARY / L2_FACT`
- 记忆压缩与事实提取的 `LLM + fallback`
- 两段式 schema 获取
  - 先做轻量表发现
  - 再只对候选表加载详细 schema
- schema 元数据缓存与 PostgreSQL 查询下推
- schema 职责解耦
  - `db_tool.py`：编排层
  - `postgres_schema_loader.py`：基础设施层
  - `schema_selection.py`：纯策略层
- 项目级 schema allowlist / denylist
- SQL 只读护栏与 aggregate/window repair
- 本地知识库、知识检索、意图级 query planner、字段 planner
- 内置调试页，聚焦每轮 SQL、LLM 分析、评审结果、Schema / Memory

## 当前目录结构

- `main.py`
- `agents/executor.py`
- `agents/reviewer.py`
- `core/workflow.py`
- `core/memory.py`
- `middleware/metrics.py`
- `schemas/models.py`
- `tools/db_tool.py`
- `tools/postgres_schema_loader.py`
- `tools/schema_selection.py`
- `tools/knowledge_retrieval.py`
- `tools/query_planner.py`
- `tools/field_planner.py`
- `tools/schema_scope.py`
- `tools/schema_term_hints.py`
- `knowledge/`
- `sql/dataanalyze_schema.sql`
- `web/index.html`

## 快速启动

1. 安装依赖

```powershell
D:/study/Python/Python3.10/python.exe -m pip install -r DataAnalyze/requirements.txt
```

2. 启动服务

```powershell
D:/study/Python/Python3.10/python.exe -m uvicorn DataAnalyze.main:app --reload --port 8100
```

3. 健康检查

```powershell
curl http://127.0.0.1:8100/health
```

4. 打开调试页

- 浏览器访问 `http://127.0.0.1:8100/debug`

## 真实数据库联调

### 1. 初始化数据库

先执行：

- `DataAnalyze/sql/dataanalyze_schema.sql`

如需样例数据，可继续执行：

- `DataAnalyze/sql/ops_log_event_sample_data.sql`
- `DataAnalyze/sql/ops_log_event_diverse_seed.sql`

### 2. 配置数据库连接

推荐使用环境变量覆盖：

- `DATAANALYZE_DB_HOST`
- `DATAANALYZE_DB_PORT`
- `DATAANALYZE_DB_NAME`
- `DATAANALYZE_DB_USER`
- `DATAANALYZE_DB_PASSWORD`
- `DATAANALYZE_DB_SCHEMA`

相关行为控制变量：

- `DATAANALYZE_SCHEMA_SCOPE_ENABLED`
- `DATAANALYZE_LLM_API_KEY`
- `DATAANALYZE_EXECUTOR_LLM_API_KEY`
- `DATAANALYZE_REVIEWER_LLM_API_KEY`
- `DATAANALYZE_SCHEMA_ALLOWLIST`
- `DATAANALYZE_SCHEMA_DENYLIST`
- `DATAANALYZE_SCHEMA_METADATA_CACHE_ENABLED`
- `DATAANALYZE_SCHEMA_METADATA_CACHE_TTL_SEC`
- `DATAANALYZE_KNOWLEDGE_EMBEDDING_ENABLED`
- `DATAANALYZE_KNOWLEDGE_EMBEDDING_BASE_URL`
- `DATAANALYZE_KNOWLEDGE_EMBEDDING_MODEL`
- `DATAANALYZE_KNOWLEDGE_EMBEDDING_TIMEOUT_SEC`
- `DATAANALYZE_KNOWLEDGE_RETRIEVAL_TOP_K`

LLM Key 规则：
- `DATAANALYZE_LLM_API_KEY`：给 executor / reviewer 共用
- `DATAANALYZE_EXECUTOR_LLM_API_KEY`：只覆盖 executor 侧
- `DATAANALYZE_REVIEWER_LLM_API_KEY`：只覆盖 reviewer 侧；未设置时会回退到 executor key，再回退到公共 key

### 3. 发起请求

```powershell
curl -X POST http://127.0.0.1:8100/chat ^
  -H "Content-Type: application/json" ^
  -d "{\"session_id\":\"real-db-1\",\"query\":\"分析最近错误日志趋势\",\"max_retries\":2}"
```

## 重启后记忆如何恢复

当前不是在服务启动时一次性把所有记忆读入内存，而是按会话懒加载：

- `main.py` 创建全局 `memory_manager = MemoryManager()`
- `WorkflowEngine.process()` 每次先执行 `memory_manager.add_message(...)`
- `add_message()` 内部先调 `_ensure_loaded(session_id)`
- `_ensure_loaded(session_id)` 从 `chat_memories` 读取该会话历史

## 当前 schema / planner 机制

当前 `/chat` 里的 schema 主链路是：

- 轻量表发现
- 项目作用域过滤
- 知识召回
- 第一阶段 query planner
- 候选表缩圈
- 候选表详情加载
- 关系扩展
- 第二阶段字段 planner
- query-aware fallback
- schema prompt + knowledge prompt

调试信息重点看：

- `tool_calls`
- `review_debug.schema_strategy`
- `review_debug.schema_fetch_mode`
- `review_debug.schema_discovery_tables`
- `review_debug.query_planner_*`
- `review_debug.column_planner_*`
- `review_debug.selected_columns_by_table`
- `review_debug.knowledge_strategy`

## 当前接口说明

- `GET /health`
- `GET /schema`
- `GET /`
- `GET /debug`
- `GET /metrics`
- `GET /sessions/{session_id}/memory`
- `POST /chat`

说明：

- `use_mock` 已移除，所有 SQL 执行统一走真实 PostgreSQL 只读路径
- `workflow_events` 当前仍保留在 `/chat` 响应里，作为兼容与排障信息
- `Session 列表` 接口已移除，调试页不再依赖它

## Metrics

可通过 `http://127.0.0.1:8100/metrics` 查看。

重点指标包括：

- `dataanalyze_http_requests_total`
- `dataanalyze_http_request_duration_seconds`
- `dataanalyze_executor_sql_generation_total`
- `dataanalyze_executor_sql_guard_total`
- `dataanalyze_executor_analysis_total`
- `dataanalyze_db_query_duration_seconds`
- `dataanalyze_schema_metadata_load_total`
- `dataanalyze_schema_metadata_cache_total`
- `dataanalyze_schema_selection_total`
- `dataanalyze_schema_fetch_total`
- `dataanalyze_query_planner_total`
- `dataanalyze_column_planner_total`
- `dataanalyze_knowledge_retrieval_total`
- `dataanalyze_memory_compression_total`
- `dataanalyze_memory_fact_extraction_total`

## 文档入口

- [implementation_stage_journal.md](./implementation_stage_journal.md)
- [docs/project_evolution/README.md](./docs/project_evolution/README.md)
## 2026-05-14 Current Tool Layout

`DataAnalyze/tools` 已按职责拆成两个子目录，后续阅读代码时优先从这里看边界：

- `tools/db/`：原 PostgreSQL/关系型数据库分析工具链，包含 `db_tool.py`、schema loader、schema selection、knowledge retrieval、query planner、field planner 等。`/chat` 和 `ExecutorAgent` 继续复用这里的 `DatabaseTool`。
- `tools/perfetto/`：Perfetto trace 分析工具链，包含 `perfetto_tool.py`、`perfetto_sources.py`、`perfetto_templates.py`。`/perfetto/query`、`/perfetto/analyze`、`/perfetto/agent` 走这里。
- `tools/llm_tool.py`：仍保留在 `tools/` 根目录，因为它是 executor、reviewer、memory 等多处共享的 LLM 辅助工具。

当前 Perfetto 主联调入口是 `POST /perfetto/agent`。v1 默认通过 `TraceProcessorPerfettoSource` 查询仓库根目录 `output.pb`，同时请求/响应保留 `dataset_id`、`trace_id`、`source_type`，方便后续切到入库后的 `DatabasePerfettoSource`。

## 2026-05-15 Perfetto LLM Config

Perfetto 双 Agent 链路预留了独立 LLM 配置，不会复用或改动 `/chat` 的 executor/reviewer key。后续 `PerfettoExecutorAgent` 会复用 `DataAnalyze/tools/llm_tool.py` 的 `LLMClient`。

```powershell
$env:DATAANALYZE_PERFETTO_LLM_ENABLED="true"
$env:DATAANALYZE_PERFETTO_LLM_BASE_URL="https://api.deepseek.com/v1"
$env:DATAANALYZE_PERFETTO_LLM_API_KEY="your-key"
$env:DATAANALYZE_PERFETTO_LLM_MODEL="deepseek-chat"
$env:DATAANALYZE_PERFETTO_LLM_TIMEOUT_SEC="60"
```

如果使用 Ollama 的 OpenAI-compatible 接口，可以改成：

```powershell
$env:DATAANALYZE_PERFETTO_LLM_BASE_URL="http://127.0.0.1:11434/v1"
$env:DATAANALYZE_PERFETTO_LLM_API_KEY="ollama"
$env:DATAANALYZE_PERFETTO_LLM_MODEL="qwen2.5:7b"
```

默认 `DATAANALYZE_PERFETTO_LLM_ENABLED=false`，且未配置 `DATAANALYZE_PERFETTO_LLM_API_KEY` 时不会启用 Perfetto LLM。

`GET /perfetto/debug` 的 `analysis_mode` 已支持 `llm`。选择 `llm` 时，`POST /perfetto/agent` 会走 `PerfettoExecutorAgent`：先让 LLM 生成 Perfetto SQL，再经 `PerfettoTool.validate_sql()` 和 trace processor 执行，最后由 LLM 汇总指标、证据和结论。未配置 Perfetto LLM 时会返回可读错误；`template` 模式仍保持原模板链路。
