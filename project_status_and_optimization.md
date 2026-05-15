## 1. 目标与落地范围

当前阶段围绕 DataAnalyze 后端完成了从“可运行骨架”到“可联调、可追踪、可回退”的实现，重点包括：

- 双 Agent 协作闭环：Executor 执行 + Reviewer 审核
- FastAPI 服务化：提供聊天、记忆、schema、debug 页面接口
- 状态机调度：执行、审核、失败重试、流程事件记录
- 记忆体系：L0/L1/L2 分层记忆 + 压缩 + 事实提取 + 持久化
- SQL 安全基线：仅允许只读 `SELECT`
- LLM 接入：Executor 与 Reviewer 独立配置、可停用、失败回退
- 内置前端调试页：聚焦聊天、Memory、Schema、LLM 分析、评审结果
- 知识库 + query planner + 字段 planner

## 2. 当前接口

- `/health`
- `/schema`
- `/`
- `/debug`
- `/metrics`
- `/sessions/{session_id}/memory`
- `/chat`

说明：

- `use_mock` 已移除，执行链路统一走真实 PostgreSQL 只读查询
- `workflow_events` 仍保留在 `/chat` 响应中，用于兼容和排障
- `Session 列表` 接口已删除

## 3. 当前优化重点

- 继续让 `query_pattern` / `metric_definition` 进入 SQL 规划层
- 优化空结果和时间窗口策略
- 保持主文件不回胖，新增能力优先放在边界清晰的 helper 中
