# Perfetto 双 Agent 接入任务书

日期：2026-05-14

## 背景

当前系统原有核心能力是面向数据库的双 Agent 链路：

```text
用户问题
 -> ExecutorAgent 生成 SQL
 -> DatabaseTool 执行 SQL
 -> ExecutorAgent 基于结果生成分析
 -> ReviewerAgent 审核 SQL / 结果 / 结论
 -> 通过或重试
```

Perfetto/output.pb 接入后，数据源从 PostgreSQL 变成 trace processor + `output.pb`，但目标流程本质不变。新的目标不是长期依赖模板分析，而是把原有双 Agent 思路迁移到 Perfetto 数据源：

```text
用户性能问题
 -> PerfettoExecutorAgent 生成 Perfetto SQL
 -> PerfettoTool 通过 trace processor 查询 output.pb
 -> PerfettoExecutorAgent 基于 rows 生成 metrics / evidence / conclusion
 -> PerfettoReviewer 审核 SQL / 指标 / 结论
 -> 通过或重试
```

## 总目标

让 `POST /perfetto/agent` 成为 Perfetto 性能分析的主入口，能够复用现有双 Agent 设计思想，跑通：

```text
性能问题 -> LLM 生成 Perfetto SQL -> SQL guard -> trace processor 执行 -> 指标/证据/结论 -> LLM/规则 review
```

同时保留当前模板链路作为 fallback，确保 LLM 未配置、LLM 失败、SQL guard 拦截时系统仍能给出可读结果。

## 设计原则

- 复用现有 `LLMClient / LLMEndpointConfig`，不新造 LLM HTTP 客户端。
- 复用 `ExecutorAgent / ReviewerAgent` 的流程思想，不直接复用 `DatabaseTool` 本体。
- `DatabaseTool` 继续只负责 PostgreSQL/关系型数据库链路。
- `PerfettoTool` 负责 Perfetto schema、Perfetto SQL guard、trace processor 查询。
- LLM 可以生成 Perfetto SQL，但必须经过 Perfetto SQL guard。
- Reviewer 必须检查结论是否被 metrics/evidence 支撑，避免空结果误报异常。
- `POST /perfetto/agent` 的请求/响应结构保持稳定，继续保留 `dataset_id / trace_id / source_type`，为后续入库数据源留空间。

## 当前已完成基线

- `DataAnalyze/tools/db/`：数据库工具链已独立成目录。
- `DataAnalyze/tools/perfetto/`：Perfetto 工具链已独立成目录。
- `PerfettoTool` 已支持：
  - 默认读取仓库根目录 `output.pb`
  - Perfetto schema prompt
  - Perfetto SQL 只读 guard
  - `execute_sql(sql)`
  - 模板分析 `analyze_problem(...)`
- `PerfettoDataSource` 已有：
  - `TraceProcessorPerfettoSource`
  - `DatabasePerfettoSource` 占位
- `POST /perfetto/query` 已可直接执行 Perfetto SQL。
- `POST /perfetto/analyze` 已可走模板分析。
- `POST /perfetto/agent` 已存在，当前 v1 走模板数据源。
- `GET /perfetto/debug` 已可前端联调。

## 任务拆分

### Step 1：复用现有 LLM 配置

目标：给 Perfetto 链路单独配置 LLM，但仍复用 `LLMClient`。

状态：已完成基础配置落地。

建议新增配置：

```text
DATAANALYZE_EXECUTOR_LLM_API_KEY
 -> fallback DATAANALYZE_LLM_API_KEY
```

验收：

- 已保留 `PERFETTO_LLM_CONFIG` 作为内部配置对象，但它复用 executor/common API key。
- 不配置 executor/common key 时，Perfetto LLM 自动视为未启用。
- 已配置原有 DB Agent LLM key 时，`PerfettoExecutorAgent` 可以通过同一个 `LLMClient` 调用模型。
- 不再要求 `DATAANALYZE_PERFETTO_LLM_*` 独立环境变量。

实现文件：

- `DataAnalyze/config.py`
- `tests/test_config_env.py`

### Step 2：新增 PerfettoExecutorAgent

目标：实现 Perfetto 版 executor，复用现有 executor 的工作方式。

状态：已完成第一版落地。当前 `analysis_mode=llm` 会走 `PerfettoExecutorAgent`；其他模式暂时保持模板链路，后续 Step 3 再完善 `auto` fallback 策略。

调试补充：`PerfettoExecutorAgent` 已把两次 LLM 解析结果写入 `tool_calls`：

- `generate_perfetto_sql.arguments.llm_response`：LLM 生成 SQL 的 JSON。
- `summarize_perfetto_result.arguments.llm_response`：LLM 汇总结果的 JSON。
- `summarize_perfetto_result.arguments.normalized_summary`：系统规范化后的最终指标、证据、结论和建议。

前端 `GET /perfetto/debug` 已增加 `Tool Calls` 页签，可直接查看上述内容。

### Step 2.5：对齐 DBTool 的 schema/knowledge/planner 输出

状态：已完成第一版。

本步骤的目标是避免 Perfetto 变成另一个孤立系统，而是复用已有知识库检索和 planner/debug 结构。

已完成：

- 新增 `DataAnalyze/knowledge/perfetto/` 知识包。
- Perfetto 核心表先按固定 trace processor 表描述：
  - `slice`
  - `thread_track`
  - `thread`
  - `process`
  - `sched`
  - `counter`
  - `counter_track`
  - `actual_frame_timeline_slice`
  - `expected_frame_timeline_slice`
- `PerfettoTool` 已复用 `KnowledgeRetriever`。
- `PerfettoTool.select_schema_context()` 已返回 DB 风格 `SchemaSelectionResult` debug 字段：
  - `fetch_mode`
  - `discovery_tables`
  - `query_planner_strategy`
  - `query_planner_primary_metric`
  - `query_planner_candidate_tables_hard`
  - `query_planner_candidate_tables_soft`
  - `column_selection_strategy`
  - `selected_columns_by_table`
  - `knowledge_strategy`
  - `knowledge_hit_ids`
  - `knowledge_column_hints`
- `PerfettoExecutorAgent` 的 `select_perfetto_schema_context` tool call 已输出上述字段，方便前端调试。

说明：

- 当前 Perfetto query planner 是规则 planner，但复用了 DB 侧 `sanitize_query_plan_output / merge_query_planner_tables / build_query_plan_summary` 等 helper。
- 当前不做真实 `output.pb` 入库，也不新增独立知识检索系统。

新增文件：

```text
DataAnalyze/agents/perfetto_executor.py
```

核心职责：

- 接收 `PerfettoAgentRequest`。
- 从 `PerfettoTool` 获取 Perfetto schema prompt。
- 调 LLM 生成 Perfetto SQL。
- 使用 `PerfettoTool.execute_sql(sql)` 执行。
- 基于查询结果调用 LLM 生成结构化分析：
  - `metrics`
  - `evidence`
  - `conclusion`
  - `recommendations`
- 写入 `tool_calls`，方便前端观察链路。

LLM 生成 SQL 的输出格式建议固定为：

```json
{
  "sql": "SELECT ...",
  "analysis_type": "long_slice",
  "reason": "..."
}
```

验收：

- `analysis_mode=llm` 时，`/perfetto/agent` 走 LLM SQL。
- LLM 返回非 JSON、空 SQL、危险 SQL 时，接口不 500。
- SQL guard 拦截时，响应 `success=false`，且 `error_reason` 可读。
- 当前暂不做 `auto` fallback，留到 Step 3。

实现文件：

- `DataAnalyze/agents/perfetto_executor.py`
- `DataAnalyze/agents/perfetto_agent.py`
- `DataAnalyze/tools/perfetto/perfetto_tool.py`
- `tests/test_perfetto_executor.py`

### Step 3：保留模板 fallback

目标：LLM 不可靠时仍保留当前可用链路。

建议规则：

- `analysis_mode=template`：强制走现有模板链路。
- `analysis_mode=llm`：强制走 LLM 链路，失败时返回错误，不静默降级。
- `analysis_mode=auto`：优先 LLM，失败后 fallback 到模板链路。

验收：

- 未配置 LLM 时，`auto` 能 fallback 到当前模板。
- `template` 行为与当前版本兼容。
- fallback 原因写入 `tool_calls` 或 `plan`，前端可见。

### Step 4：升级 PerfettoReviewer 为规则 + 可选 LLM

目标：让 Perfetto 也具备类似 DB reviewer 的二阶段审核。

规则审核继续保留：

- SQL 非空。
- metrics 非空。
- evidence 和 conclusion 至少有一个有内容。
- `source_type` 合法。
- 单位字段为 `dur_ms / cpu_time_ms` 时，metric unit 必须是 `ms`。
- 空结果时 conclusion 不能误报异常。

新增 LLM review：

- 输入 problem、sql、metrics、evidence、conclusion、recommendations、row_count。
- 输出：

```json
{
  "approved": true,
  "reason": "...",
  "should_retry": false
}
```

验收：

- LLM review 未启用时，规则审核照常工作。
- LLM review 启用时，review 结果包含 mode/debug 信息。
- LLM review 失败时 fallback 到规则审核，不让接口裸 500。

### Step 5：加入重试闭环

目标：Reviewer 不通过时，允许 PerfettoExecutorAgent 带着 review reason 重写 SQL。

第一版建议只做一次 retry：

```text
LLM SQL -> execute -> analyze -> review fail
 -> retry prompt 带上 review reason 和上一轮 SQL
 -> regenerate SQL
 -> execute -> analyze -> final review
```

验收：

- `/perfetto/agent` 响应能看到每轮 tool call。
- retry 不超过配置上限。
- retry 后仍失败时，返回最后一次 review reason。

### Step 6：前端调试页增强

目标：让 `GET /perfetto/debug` 能观察 LLM 链路。

增强项：

- `analysis_mode` 增加 `llm`。
- 展示 executor strategy：
  - `llm`
  - `template`
  - `llm_fallback_template`
- 展示 LLM SQL 生成 reason。
- 展示 review mode：
  - `rule`
  - `llm`
  - `llm_fallback_rule`
- 展示 retry/tool_calls 时间线。

验收：

- 页面能直接切换 template/llm/auto。
- 页面能看见 SQL、metrics、evidence、conclusion、review、raw JSON。
- LLM 失败时页面能展示 fallback/error reason。

### Step 7：文档和测试同步

目标：每一步实现后同步维护文档和最小回归测试。

需要更新：

- `02_系统现状与主链路.md`
- `03_演进路线图.md`
- `05_近期行动清单.md`
- 本任务书

建议测试：

- `python -B -c "import DataAnalyze.main"`
- `/perfetto/query` 固定 SQL
- `/perfetto/agent` template 请求
- `/perfetto/agent` llm 请求
- `/perfetto/agent` auto fallback 请求
- SQL guard 拦截危险 SQL
- Reviewer 对空结果的审核

## 目标文件边界

预计新增/修改：

- `DataAnalyze/config.py`
  - 增加 Perfetto LLM 配置。
- `DataAnalyze/agents/perfetto_executor.py`
  - 新增 Perfetto executor。
- `DataAnalyze/agents/perfetto_agent.py`
  - 从模板 facade 升级为协调 executor + reviewer + fallback。
- `DataAnalyze/agents/perfetto_reviewer.py`
  - 增加可选 LLM review。
- `DataAnalyze/tools/perfetto/perfetto_tool.py`
  - 保持执行层职责，必要时补充 schema prompt 和 guard debug。
- `DataAnalyze/web/perfetto.html`
  - 增加 LLM 模式和 tool_calls 展示。
- `DataAnalyze/schemas/models.py`
  - 如有必要，补充 debug 字段，但优先保持现有响应兼容。

## 风险点

- LLM 可能生成不合法 Perfetto SQL。
- LLM 可能引用不存在的表或字段。
- LLM 可能把 ns/ms 单位弄错。
- 空结果时 LLM 可能误判为无异常或强行编造结论。
- trace processor 查询大表时可能慢，需要保留 LIMIT 和输出行数控制。
- 后续入库数据源接入后，同一个问题可能要在 `trace_processor` 和 `database` 两种 source 上执行不同 SQL。

## 阶段性完成定义

第一阶段完成定义：

- `/perfetto/agent` 支持 `analysis_mode=llm`。
- LLM 能基于 Perfetto schema 生成 SQL。
- SQL 经过 Perfetto guard 后执行。
- 结果能由 LLM 汇总为 metrics/evidence/conclusion/recommendations。
- PerfettoReviewer 至少完成规则审核。
- `analysis_mode=auto` 能在 LLM 不可用时 fallback 到模板链路。

第二阶段完成定义：

- PerfettoReviewer 支持可选 LLM review。
- review fail 后支持一次 retry。
- 前端页面能完整展示 LLM SQL、执行结果、review、retry/tool_calls。

第三阶段完成定义：

- 接入更多 Perfetto 分析场景。
- 为后续 `output.pb -> database` 抽取后的查询切换保留数据源策略。
- 多 trace 对比/历史趋势时可以走 `DatabasePerfettoSource`。
## 2026-05-15 调整：改为直接复用原双 Agent

最新方向已经收敛为：不要把 Perfetto 做成另一套长期并行系统，而是让原有 `ExecutorAgent / ReviewerAgent` 面向不同 SQL 工具运行。

当前落地状态：

- `/chat` 仍然是 `ExecutorAgent + DatabaseTool + ReviewerAgent`。
- `/perfetto/agent` 的 `analysis_mode=llm` 已改为 `ExecutorAgent + PerfettoTool + ReviewerAgent`。
- `ExecutorAgent` 新增 `dialect/tool_label` 参数，默认仍是 PostgreSQL 行为；当 `dialect="perfetto"` 时：
  - schema 选择调用 `PerfettoTool.select_schema_context()`。
  - SQL 执行调用 `PerfettoTool.execute_sql()`。
  - SQL guard 调用 `PerfettoTool.validate_sql()`，允许只读 `SELECT/WITH`，也允许只读查询前的 `INCLUDE PERFETTO MODULE ...`。
  - prompt 会明确 Perfetto 时间单位通常是 ns，展示 ms 时需要除以 `1e6`。
- `ReviewerAgent` 新增可选 `sql_validator`，Perfetto 链路传入 `PerfettoTool.validate_sql`，所以 reviewer 不再写死“SQL 必须 SELECT 开头”，而是检查“工具侧只读策略是否通过”。
- `PerfettoAgent` 保留为前端响应适配层：把原 `AgentResponse` 转成 `PerfettoAgentResponse`，稳定输出 `dataset_id/source_type/plan/metrics/evidence/conclusion/tool_calls/review`。
- `PerfettoExecutorAgent` 目前只作为历史实验实现保留，主接线不再使用它。

后续任务优先级：

1. 把 `analysis_mode=auto` 调整为优先走原 `ExecutorAgent + PerfettoTool`，失败后 fallback 到模板链路。
2. 让 Perfetto 链路也进入类似 `WorkflowEngine` 的 retry 闭环，而不是只跑单轮。
3. 继续补 Perfetto 知识库，让原 schema/knowledge/planner 调试字段更接近 DB 链路。
4. 后续 `output.pb -> database` 入库时，仅新增/切换数据源工具，不重写 agent 流程。
