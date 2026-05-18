# Perfetto 复用原双 Agent 当前说明

日期：2026-05-15

## 当前结论

Perfetto 不再按“新建一套长期独立的 PerfettoExecutorAgent / PerfettoReviewer”作为主方向推进。当前主接线已经改成直接复用原双 Agent，只替换 SQL 工具和 SQL 安全策略。

```text
/perfetto/agent(analysis_mode=llm)
 -> PerfettoAgent 前端响应适配
 -> WorkflowEngine
 -> ExecutorAgent(dialect="perfetto")
 -> PerfettoTool.select_schema_context()
 -> LLM 生成 Perfetto SQL
 -> PerfettoTool.validate_sql()
 -> PerfettoTool.execute_sql()
 -> ExecutorAgent 汇总查询结果
 -> ReviewerAgent(sql_validator=PerfettoTool.validate_sql)
 -> 失败时带 review/error reason 重试
 -> PerfettoAgentResponse
```

`/chat` 主链路不变：

```text
/chat
 -> WorkflowEngine
 -> ExecutorAgent(dialect="postgres")
 -> DatabaseTool
 -> ReviewerAgent()
```

## 代码边界

- `ExecutorAgent`：仍是统一执行器。新增 `dialect/tool_label` 参数，默认保持 PostgreSQL 行为；Perfetto 链路传 `dialect="perfetto"`。
- `ReviewerAgent`：仍是统一审核器。新增 `sql_validator` 钩子；Perfetto 链路传入 `PerfettoTool.validate_sql`。
- `PerfettoTool`：负责 Perfetto schema、知识库召回、两段 LLM planner、SQL guard、trace processor 执行。
- `PerfettoAgent`：只负责把原 `AgentResponse` 适配成前端需要的 `PerfettoAgentResponse`，保留 `dataset_id / trace_id / source_type`。
- `WorkflowEngine`：Perfetto LLM 模式已接入完整 retry 闭环，执行失败或 reviewer 拒绝时会把原因作为 `last_error` 传给下一轮 `ExecutorAgent`。
- `PerfettoExecutorAgent`：只保留为历史实验代码，主链路不再实例化。

## Planner 对齐

Perfetto 链路现在和 DBTool 一样包含两段 planner：

```text
KnowledgeRetriever
 -> query_planner_client(LLM) 规划 query_type / primary_metric / hard-soft tables
 -> sanitize_query_plan_output()
 -> merge_query_planner_tables()
 -> column_planner_client(LLM) 规划 required/optional columns
 -> sanitize_planner_output()
 -> build_final_column_priorities()
 -> schema prompt
```

LLM 不可用或输出不可用时，`PerfettoTool` 会 fallback 到现有规则 planner 和基础列优先级，保证接口仍可运行。

## 结果汇总解析策略

Perfetto 结果汇总阶段保留 LLM 文本结论，但不再要求 LLM 输出完整 `chart` 嵌套对象。

当前策略：

- 使用 text 模式调用 LLM，再从 assistant 内容中解析 JSON。
- 规避部分 OpenAI-compatible provider 在 `response_format=json_object` 下返回坏顶层 JSON 的问题。
- LLM 只负责 `text_reply / professional_findings / recommendations`。
- chart 仍保留，由系统根据 rows 生成 `ChartConfig`。
- `tool_calls` 中的 `summarize_result.arguments` 会记录 `strategy / llm_error / raw_llm_response / fallback_fields / chart_source`。

## SQL 审核差异

DB 链路默认仍要求 SQL 以 `SELECT` 开头。

Perfetto 链路不再写死 `SELECT` 开头，而是通过 `PerfettoTool.validate_sql()` 判断只读：

- 允许 `SELECT ...`
- 允许 `WITH ... SELECT ...`
- 允许只读查询前的 `INCLUDE PERFETTO MODULE ...`
- 拦截 `INSERT / UPDATE / DELETE / DROP / CREATE / ALTER` 等写入或 DDL
- 拦截注释和不安全多语句

## 配置

Perfetto LLM 复用原 executor/common API key：

```text
DATAANALYZE_EXECUTOR_LLM_API_KEY
 -> fallback DATAANALYZE_LLM_API_KEY
```

不需要 `DATAANALYZE_PERFETTO_LLM_*`。

## 调试

前端页面：

```text
http://127.0.0.1:8100/perfetto/debug
```

最小请求：

```powershell
curl.exe -X POST http://127.0.0.1:8100/perfetto/agent `
  -H "Content-Type: application/json" `
  -d "{\"session_id\":\"s1\",\"problem\":\"分析主线程卡顿\",\"analysis_mode\":\"llm\",\"limit\":20}"
```

重点看响应里的：

- `plan.selected_tables`
- `plan.knowledge_hit_ids`
- `plan.sql_guard`
- `sql`
- `tool_calls`
- `review`

## 下一步

1. `analysis_mode=auto` 改为优先走 LLM，失败后 fallback 模板。
2. 继续补 Perfetto 知识库，让 schema/planner/debug 字段更接近 DB 链路。
3. 后续做 `output.pb -> database` 时，只新增/切换数据源工具，不重写 Agent。
