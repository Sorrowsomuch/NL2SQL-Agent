# DataAnalyze 分阶段实施记录

这个文档用于记录每个开发阶段完成了什么、改了哪些地方、如何验证，以及还剩下什么，尽量避免项目逐步变成黑盒。

## 阶段一：结构化 Schema 元数据层
日期：2026-04-21

本阶段完成内容：
- 将数据库 schema 从“单个大字符串”升级为结构化元数据对象。
- 保留旧的 `DatabaseTool.get_schema()` 兼容入口，不破坏原有调用链。
- 保持 SQL 只读护栏不变。
- 补充 schema 元数据加载与渲染相关 metrics。
- 增加最小回归测试，覆盖 schema 元数据与 SQL 护栏。

本阶段修改文件：
- `DataAnalyze/schemas/models.py`
  新增 `SchemaColumn`、`SchemaIndex`、`SchemaRelationship`、`TableSchema`、`DatabaseSchemaMetadata`。
- `DataAnalyze/tools/db_tool.py`
  新增 PostgreSQL 结构化 schema 元数据读取、builtin fallback、schema prompt 渲染。
- `DataAnalyze/middleware/metrics.py`
  新增 schema 元数据加载与渲染 metrics。
- `tests/test_schema_metadata.py`
  新增结构化 schema 基础测试。

行为说明：
- `/chat` 响应结构保持兼容。
- SQL 执行仍然只允许单条 `SELECT`，不允许注释、多语句和危险关键字。
- builtin fallback schema 中已经包含部分字段语义提示与可推断关系。

验证证据：
- `python -m unittest discover -s tests -p "test_*.py"`：通过
- 使用 `compile(...)` 对 `DataAnalyze` 和 `tests` 做语法检查：通过
- `import DataAnalyze.main`：通过

阶段遗留：
- executor 当时仍然是一次性消费 schema 文本，尚未实现 query-aware 的 schema 缩圈。
- 大 schema 的上下文膨胀问题还没有解决。

## 阶段二：Executor 渐进式 Schema 选择
日期：2026-04-21

本阶段完成内容：
- 将 executor 从直接调用 `get_schema()` 改为先执行 `select_schema_context(...)`。
- 增加渐进式 schema 选择机制：
  - 首轮尽量使用更克制的 `progressive-focused`
  - 重试时放宽为 `progressive-expanded`
- 将选中的表、关系、prompt 预算写入 `tool_calls` 和 `review_debug`。
- 增加一个可人工维护的领域词到候选表映射入口，帮助你逐步教系统认业务词。
- 保持 `/chat` 响应结构兼容，保持 SQL 只读护栏不变。

本阶段修改文件：
- `DataAnalyze/schemas/models.py`
  新增 `SchemaSelectionResult`。
- `DataAnalyze/tools/db_tool.py`
  新增 query-aware 的表排序、关系扩展、schema 裁剪与 prompt 预算控制。
- `DataAnalyze/tools/schema_term_hints.py`
  新增可手工维护的“词 -> 表”提示映射。
- `DataAnalyze/agents/executor.py`
  改造 executor，使其在生成 SQL 前先消费渐进式 schema 选择结果。
- `DataAnalyze/agents/reviewer.py`
  在 `review_debug` 中补充 schema 策略、选中表、关系、prompt 预算。
- `DataAnalyze/middleware/metrics.py`
  新增 schema selection metrics。
- `tests/test_schema_metadata.py`
  新增 `focused / expanded` 两类 schema 选择测试。

行为说明：
- 首轮发给 LLM 的 schema prompt 更小，更符合“渐进式提供”的目标。
- 重试时会适当扩大表数与列数预算。
- fallback SQL 已支持基础 `chat_memories + chat_sessions` 关联路径。

验证证据：
- `python -m unittest discover -s tests -p "test_*.py"`：通过，`Ran 7 tests ... OK`
- 使用 `compile(...)` 对 `DataAnalyze` 和 `tests` 做语法检查：通过，`SYNTAX_OK 14 files`
- 工作流冒烟：`workflow.process(..., query='查看会话记忆和 session 标题', max_retries=0)` 通过

你可以参与修改的入口：
- `DataAnalyze/tools/schema_term_hints.py`
- 这是目前最适合你亲手维护的业务词入口，风险低，不会碰 SQL 只读护栏。

阶段遗留：
- 字段语义仍然主要靠字段名和 hints，不够强。
- 阶段三“字段语义知识库 / RAG”尚未开始。

## 阶段二补强：SQL 生成稳定性防抖
日期：2026-04-22

本阶段完成内容：
- 针对 PostgreSQL 常见错误 `aggregate function calls cannot contain window function calls`，在 executor 中增加 SQL 生成后的本地校验。
- 增加“先校验、再修复、最后执行”的 SQL guard 链路。
- 当命中“聚合函数参数内嵌窗口函数”风险时，优先要求模型改写为“外层 `SELECT` + 内层子查询”的结构。
- 保持 SQL 只读护栏不变，不引入 `WITH` 开头的 CTE，以避免和当前安全策略冲突。
- 将 SQL guard 结果写入 `tool_calls`、metrics、`review_debug`。

本阶段修改文件：
- `DataAnalyze/agents/executor.py`
  新增 SQL 校验、aggregate/window 冲突检测、修复提示与 SQL guard trace。
- `DataAnalyze/middleware/metrics.py`
  新增 `dataanalyze_executor_sql_guard_total`。
- `DataAnalyze/agents/reviewer.py`
  在 `review_debug` 中新增 `sql_guard_outcome`、`sql_guard_reason`。
- `tests/test_executor_sql_guard.py`
  新增 aggregate/window 冲突检测与修复回归测试。

行为说明：
- LLM 即使先生成了高风险 SQL，也会先经过本地 guard。
- 校验失败时优先修复，修复失败才回退 fallback SQL。
- `review_debug` 中可直接看到本轮 SQL guard 的结果。

验证证据：
- `python -m unittest discover -s tests -p "test_*.py"`：通过，`Ran 10 tests ... OK`
- 使用 `compile(...)` 对 `DataAnalyze` 和 `tests` 做语法检查：通过，`SYNTAX_OK 15 files`
- 工作流冒烟：`workflow.process(..., query='分析最近错误日志趋势', max_retries=1)` 通过

阶段遗留：
- 这一步只补强了 SQL 生成稳定性，还没有改造记忆压缩与事实提取。
- 阶段三“字段语义知识库 / RAG”尚未开始。

## 阶段二补强：记忆压缩与事实提取 LLM 化
日期：2026-04-22

本阶段完成内容：
- 将 `compress_context()` 升级为“优先 LLM 摘要，失败时 fallback 到规则摘要”的混合模式。
- 将 `extract_facts_if_needed()` 升级为“优先 LLM 结构化抽取，失败时 fallback 到规则抽取”的混合模式。
- 保持 `L0_RAW / L1_SUMMARY / L2_FACT` 的分层结构不变。
- 将记忆层策略写入 metrics，并合并进 `/chat` 的 `review_debug`，便于前后端联调时观察。
- 保持数据库记忆恢复路径不变：仍由 `MemoryManager._ensure_loaded(session_id)` 在会话首次访问时从 `chat_memories` 懒加载到内存。

本阶段修改文件：
- `DataAnalyze/core/memory.py`
  新增 LLM 摘要与结构化事实抽取逻辑，并保留 fallback。
- `DataAnalyze/core/workflow.py`
  合并 reviewer debug 与 memory debug，同时补充“重启后懒加载记忆”的中文说明。
- `DataAnalyze/middleware/metrics.py`
  新增记忆压缩与事实抽取相关 metrics。
- `tests/test_memory_llm_pipeline.py`
  新增 LLM 成功、LLM 失败 fallback、事实抽取写入 L2 的回归测试。

行为说明：
- `compress_context()` 先尝试让 LLM 输出结构化摘要，再渲染成稳定文本。
- `extract_facts_if_needed()` 优先让 LLM 输出结构化 `facts`，再去重并落到 `L2_FACT`。
- 当 LLM 不可用、超时或返回格式异常时，流程不会中断，会自动回退到本地规则逻辑。
- `review_debug` 新增以下记忆层字段：
  - `memory_summary_strategy`
  - `memory_summary_reason`
  - `memory_fact_strategy`
  - `memory_fact_reason`
  - `memory_fact_count`

验证证据：
- `python -m unittest tests.test_memory_llm_pipeline`：通过，`Ran 4 tests ... OK`
- `python -m unittest discover -s tests -p "test_*.py"`：通过，`Ran 14 tests ... OK`
- 使用 `compile(...)` 对 `DataAnalyze` 和 `tests` 做语法检查：通过，`SYNTAX_OK 16 files`
- 工作流冒烟：
  `workflow.process(session_id='memory-stage-smoke', query='请统计错误日志数量，并保持 SQL 只读护栏不变', max_retries=0)` 通过

你可以参与修改的入口：
- 如果你想亲手微调摘要格式，可以从 `DataAnalyze/core/memory.py` 的 `_build_summary_with_llm()` 开始改 JSON 字段。
- 如果你想亲手微调事实类型约束，可以从 `DataAnalyze/core/memory.py` 的 `_extract_facts_with_llm()` 开始改 `fact_type` 约束。
- 这两个入口都比较安全，不会碰 SQL 只读护栏。

阶段遗留：
- 这一步完成的是会话记忆层，不是字段语义知识库。
- 阶段三“字段语义知识库 / RAG”尚未开始。

## 阶段二补强：项目级 Schema Allowlist
日期：2026-04-22

本阶段完成内容：
- 新增项目级 schema allowlist，让渐进式 schema 选择默认只关注 DataAnalyze 当前核心表。
- 在混合数据库环境中，即使存在别的项目表，也会优先在 schema 元数据层把无关表过滤掉。
- 支持通过环境变量覆盖 allowlist / denylist，便于你后续联调不同数据库实例。
- 不改 SQL 只读护栏，不改 `/chat` 返回结构。

本阶段修改文件：
- `DataAnalyze/tools/schema_scope.py`
  新增项目级 schema allowlist / denylist 配置入口。
- `DataAnalyze/tools/db_tool.py`
  在 schema 元数据返回前增加项目级作用域过滤。
- `DataAnalyze/tools/schema_term_hints.py`
  重写为中文注释版本，保留可手工维护的词表入口。
- `tests/test_schema_metadata.py`
  新增 allowlist 过滤与环境变量覆盖测试。

行为说明：
- 默认 allowlist 只包含：
  - `chat_sessions`
  - `chat_memories`
  - `ops_log_event`
- 如果数据库里混入了 `agent` 这类无关表，它们会在 schema 元数据层就被挡掉，不再参与选表和关系扩展。
- 如需临时切换范围，可使用：
  - `DATAANALYZE_SCHEMA_SCOPE_ENABLED`
  - `DATAANALYZE_SCHEMA_ALLOWLIST`
  - `DATAANALYZE_SCHEMA_DENYLIST`

验证证据：
- `python -m unittest tests.test_schema_metadata`：通过，`Ran 9 tests ... OK`
- `python -m unittest discover -s tests -p "test_*.py"`：通过，`Ran 16 tests ... OK`
- 使用 `compile(...)` 对 `DataAnalyze` 和 `tests` 做语法检查：通过，`SYNTAX_OK 17 files`
- 冒烟验证：向 builtin schema 人工注入 `agent` 后，作用域过滤结果仍只保留 `chat_sessions / chat_memories / ops_log_event`

你可以参与修改的入口：
- `DataAnalyze/tools/schema_scope.py`
- 这是这一步最适合你亲手维护的入口。
- 如果你后面要接自己的业务表，优先往这里补 allowlist，而不是去删数据库里的别的项目表。

阶段遗留：
- 这一步解决的是“本项目关注表范围”问题，不是字段语义理解问题。
- 阶段三“字段语义知识库 / RAG”尚未开始。

## 阶段三起步：知识库目录与示例骨架
日期：2026-04-23

本阶段完成内容：
- 在项目内新增第一版知识库目录骨架，作为后续 RAG 与字段语义理解的基础数据层。
- 固定知识对象类型：
  - `table_profile`
  - `column_semantics`
  - `relationship_hint`
  - `metric_definition`
  - `query_pattern`
  - `glossary`
- 采用文件化维护方式，先不接入运行链路，避免阶段三一开始就把改动面铺太大。
- 明确当前 embedding 模型约定为 `bge-m3`。

本阶段修改文件：
- `DataAnalyze/knowledge/README.md`
  新增知识库包说明、目录用途与后续接入建议。
- `DataAnalyze/knowledge/manifest.yaml`
  新增知识库总清单与检索阶段约定。
- `DataAnalyze/knowledge/tables/*.yaml`
  新增三张核心表的表级知识。
- `DataAnalyze/knowledge/columns/*.yaml`
  新增关键字段语义知识示例。
- `DataAnalyze/knowledge/relationships/*.yaml`
  新增记忆表到会话表的关系提示。
- `DataAnalyze/knowledge/metrics/*.yaml`
  新增错误数与记忆条数的指标定义示例。
- `DataAnalyze/knowledge/patterns/*.yaml`
  新增错误趋势与会话记忆查看两类问法模式。
- `DataAnalyze/knowledge/glossary/business_terms.yaml`
  新增业务术语与知识对象映射示例。
- `tests/test_knowledge_pack.py`
  新增知识包存在性与基础结构测试。

行为说明：
- 当前知识库仅作为静态知识资产存在，还没有接入 executor / db_tool / reviewer。
- 目录和示例条目已经按后续 RAG 可直接消费的粒度拆分，不需要以后再从大文档里反切。
- 当前建议的检索顺序是：
  1. `tables + metrics + patterns`
  2. `columns + relationships`

验证证据：
- `python -m unittest tests.test_knowledge_pack`：通过，`Ran 3 tests ... OK`
- `python -m unittest discover -s tests -p "test_*.py"`：通过
- 使用 `compile(...)` 对 `DataAnalyze` 和 `tests` 做语法检查：通过

你可以参与修改的入口：
- `DataAnalyze/knowledge/columns/`
  最适合你亲手补“字段名和真实业务含义不一致”的映射。
- `DataAnalyze/knowledge/patterns/`
  最适合你补“真实用户常问但模型容易选错表”的问法模式。
- `DataAnalyze/knowledge/glossary/business_terms.yaml`
  最适合你补业务别名和领域词。

阶段遗留：
- 这一步只完成了知识资产落盘，尚未做加载器、embedding、向量检索和 rerank。
- 阶段三“字段语义知识库 / RAG”已经开始，但还没有真正接入运行链路。

## 阶段三接线：知识库加载器、本地检索与 bge-m3 接入
日期：2026-04-23

本阶段完成内容：
- 新增知识库加载器，从 `DataAnalyze/knowledge/` 目录扫描知识条目。
- 新增本地检索器，支持：
  - 词法检索
  - 本地 Ollama embedding 检索
  - 混合召回
- 将本地 Ollama `bge-m3` 作为默认 embedding 模型接入知识召回链路。
- 将知识检索结果接入 `db_tool.select_schema_context()`，用于辅助表排序与 prompt 补充。
- 将知识检索策略和命中结果透传到 `tool_calls` 与 `review_debug`。
- 增加知识检索相关 metrics。

本阶段修改文件：
- `DataAnalyze/config.py`
  新增知识检索 embedding 配置。
- `DataAnalyze/tools/knowledge_retrieval.py`
  新增知识加载器、Ollama embedding 客户端、本地检索器。
- `DataAnalyze/tools/db_tool.py`
  将知识召回结果接入 schema 选择链路，并将知识提示补进 prompt。
- `DataAnalyze/schemas/models.py`
  在 `SchemaSelectionResult` 中新增知识检索相关字段。
- `DataAnalyze/agents/executor.py`
  将知识检索字段写入 `select_schema_context` 的 tool trace。
- `DataAnalyze/agents/reviewer.py`
  将知识检索字段补充进 `review_debug`。
- `DataAnalyze/middleware/metrics.py`
  新增知识检索 metrics。
- `tests/test_knowledge_retrieval.py`
  新增知识加载、词法检索、fake bge-m3 混合检索、schema 选择接线测试。
- `tests/test_schema_metadata.py`
  调整测试环境，默认关闭本地 embedding，避免回归受本地 Ollama 状态影响。
- `tests/test_executor_sql_guard.py`
  同步关闭本地 embedding，保证 SQL guard 回归稳定。

行为说明：
- 当本地 Ollama 可用时，知识检索会优先走 `hybrid-bge-m3`。
- 当本地 Ollama 不可用时，会自动回退到 `lexical` 或 `lexical-fallback`，不会阻断主链路。
- 知识召回结果当前主要用于：
  - 辅助表排序
  - 生成额外的 knowledge prompt 段落
- 当前还没有做向量持久化与 rerank，属于第一版在线检索接线。

新增环境变量：
- `DATAANALYZE_KNOWLEDGE_EMBEDDING_ENABLED`
- `DATAANALYZE_KNOWLEDGE_EMBEDDING_BASE_URL`
- `DATAANALYZE_KNOWLEDGE_EMBEDDING_MODEL`
- `DATAANALYZE_KNOWLEDGE_EMBEDDING_TIMEOUT_SEC`
- `DATAANALYZE_KNOWLEDGE_RETRIEVAL_TOP_K`

新增观测字段：
- metrics
  - `dataanalyze_knowledge_retrieval_total`
  - `dataanalyze_knowledge_retrieval_hits`
- `review_debug`
  - `knowledge_strategy`
  - `knowledge_hit_ids`
  - `knowledge_hit_titles`

验证证据：
- `python -m unittest tests.test_knowledge_retrieval`：通过，`Ran 4 tests ... OK`
- `python -m unittest discover -s tests -p "test_*.py"`：通过，`Ran 23 tests ... OK`
- 使用 `compile(...)` 对 `DataAnalyze` 和 `tests` 做语法检查：通过，`SYNTAX_OK 20 files`

阶段遗留：
- 当前文档解析仍是轻量级文本提取，不是完整 YAML 语义解析器。
- 还没有做 embedding 缓存落盘、向量索引和 rerank。
- 还没有把知识检索真正用于字段级 SQL 规划，只先接到了 schema 召回层。

## 阶段 3 补强：Schema 元数据缓存与查询下推
日期：2026-04-24

本阶段完成内容：
- 为 `get_schema_metadata()` 增加实例内 TTL 缓存，避免重复请求反复扫描 schema 元数据。
- 为 schema 元数据缓存增加独立 metrics，区分命中、未命中、过期和写入。
- 让 `_load_schema_metadata_from_postgres(table_names)` 真正把表过滤下推到 PostgreSQL 元数据查询层。
- 当未显式传入 `table_names` 时，优先使用项目 allowlist 作为元数据预过滤范围；若预过滤没有拿到结果，再自动回退到原始范围，兼容旧行为。
- 补充回归测试，覆盖缓存复用、allowlist 预过滤与 PostgreSQL 查询下推。

本阶段修改文件：
- `DataAnalyze/config.py`
  新增 schema 元数据缓存配置。
- `DataAnalyze/middleware/metrics.py`
  新增 schema 元数据缓存 metrics。
- `DataAnalyze/tools/db_tool.py`
  新增 schema 元数据缓存、allowlist 预过滤与 PostgreSQL 查询级表过滤下推。
- `tests/test_schema_metadata.py`
  新增缓存复用、预过滤与查询下推测试。
- `DataAnalyze/README.md`
  同步当前能力与环境变量说明。

行为说明：
- 相同表范围的 schema 元数据请求会优先命中缓存。
- 默认场景下，如果项目 allowlist 已配置，会先只查询 allowlist 内的表结构。
- 如果 allowlist 预过滤因为环境差异没有拿到任何元数据，会自动回退到原始查询范围，不直接把结果判成失败。
- `table_names` 现在不再只是 Python 层裁剪，而会直接影响 `information_schema` 与 `pg_catalog` 查询范围。

新增环境变量：
- `DATAANALYZE_SCHEMA_METADATA_CACHE_ENABLED`
- `DATAANALYZE_SCHEMA_METADATA_CACHE_TTL_SEC`

新增观测字段：
- metrics
  - `dataanalyze_schema_metadata_cache_total`

验证证据：
- `python -m unittest tests.test_schema_metadata`：通过，`Ran 12 tests ... OK`
- `python -m unittest discover -s tests -p "test_*.py"`：通过，`Ran 26 tests ... OK`
- 使用 `compile(...)` 对 `DataAnalyze` 和 `tests` 做语法检查：通过，`SYNTAX_OK 20 files`

阶段遗留：
- 当前缓存还是进程内缓存，服务重启后不会保留。
- 当前还没有把 schema 获取彻底拆成“轻量表摘要获取 + 候选表详情获取”两段式。

## 阶段 3 补强：两段式 Schema 获取
日期：2026-04-24

本阶段完成内容：
- 在 `select_schema_context()` 中接入“两段式 schema 获取”。
- 第一段只获取轻量表清单，用于知识召回和候选表排序。
- 第二段只对候选表加载详细 schema，再做关系扩展、列裁剪和 prompt 渲染。
- 为 schema 获取模式增加 metrics。
- 为 `review_debug` 增加 `schema_fetch_mode` 和 `schema_discovery_tables`，便于联调时判断是否真的走了候选表详情加载。

本阶段修改文件：
- `DataAnalyze/tools/db_tool.py`
  新增轻量表发现与两段式 schema 获取主链路。
- `DataAnalyze/middleware/metrics.py`
  新增 schema fetch mode metrics。
- `DataAnalyze/schemas/models.py`
  在 `SchemaSelectionResult` 中新增 `fetch_mode` 与 `discovery_tables`。
- `DataAnalyze/agents/executor.py`
  将两段式 schema 获取信息写入 `select_schema_context` trace。
- `DataAnalyze/agents/reviewer.py`
  将 `schema_fetch_mode` 与 `schema_discovery_tables` 写入 `review_debug`。
- `tests/test_schema_metadata.py`
  新增两段式 schema 获取行为测试。
- `DataAnalyze/README.md`
  同步当前能力说明。

行为说明：
- 当前 schema 处理从“全量详细 schema + 使用时裁剪”进一步演进为“轻量表发现 + 候选表详情加载”。
- 这一步没有改变 SQL 只读护栏，也没有改变 `/chat` 返回结构。
- 当前两段式仍然是基础版，轻量发现阶段主要依赖表名、作用域、知识召回和现有 hints。

新增观测字段：
- metrics
  - `dataanalyze_schema_fetch_total`
- `review_debug`
  - `schema_fetch_mode`
  - `schema_discovery_tables`

验证证据：
- `python -m unittest tests.test_schema_metadata`：通过，`Ran 13 tests ... OK`
- `python -m unittest discover -s tests -p "test_*.py"`：通过，`Ran 27 tests ... OK`
- 使用 `compile(...)` 对 `DataAnalyze` 和 `tests` 做语法检查：通过，`SYNTAX_OK 20 files`

阶段遗留：
- 轻量发现阶段当前还是表级，不是字段级。
- 当前还没有独立 planner，候选表仍然主要依赖现有排序逻辑。

## 阶段 3 补强：db_tool 轻量瘦身
日期：2026-04-24

本阶段完成内容：
- 将 PostgreSQL 连接、轻量表发现、详细 schema 加载等 SQL 细节从 `db_tool.py` 下沉到 `postgres_schema_loader.py`。
- `db_tool.py` 保留缓存、作用域、知识召回、schema 选择和 SQL 安全等主链路编排逻辑。
- 在关键编排节点补充中文注释，方便后续审计。

本阶段修改文件：
- `DataAnalyze/tools/postgres_schema_loader.py`
  新增 PostgreSQL schema 加载 helper。
- `DataAnalyze/tools/db_tool.py`
  精简连接与 schema 加载实现，保留主流程编排。

行为说明：
- 本阶段主要是代码组织优化，不改变 schema 选择策略，不改变 SQL 只读护栏，不改变 `/chat` 返回结构。
- 测试继续沿用原有行为验证，保证这次调整只影响可维护性，不影响基线能力。

验证证据：
- `python -m unittest tests.test_schema_metadata`：通过，`Ran 13 tests ... OK`
- `python -m unittest discover -s tests -p "test_*.py"`：通过，`Ran 27 tests ... OK`
- 使用 `compile(...)` 对 `DataAnalyze` 和 `tests` 做语法检查：通过，`SYNTAX_OK 21 files`

## 阶段 3 补强：Schema 选择逻辑继续解耦
日期：2026-04-24

本阶段完成内容：
- 将表排序、关系扩展、局部 schema 裁剪、query hints 和切词逻辑从 `db_tool.py` 抽到 `schema_selection.py`。
- `db_tool.py` 继续保留薄包装方法，保证现有测试入口和主链路不变。
- 保持“编排在 `db_tool`，纯逻辑在 helper”的边界，让主链路代码更接近常见后端服务层写法。

本阶段修改文件：
- `DataAnalyze/tools/schema_selection.py`
  新增 schema 选择纯逻辑 helper。
- `DataAnalyze/tools/db_tool.py`
  改为调用 `schema_selection.py` 中的纯函数。

行为说明：
- 本阶段继续以可维护性优化为主，不改变 schema 选择策略，不改变 metrics 含义，不改变 `/chat` 返回结构。
- 当前没有强行引入 async，是因为这部分主要是纯内存选择逻辑，异步不会带来明显收益，反而会扩大改动面。

验证证据：
- `python -m unittest tests.test_schema_metadata`：通过，`Ran 13 tests ... OK`
- `python -m unittest discover -s tests -p "test_*.py"`：通过，`Ran 27 tests ... OK`
- 使用 `compile(...)` 对 `DataAnalyze` 和 `tests` 做语法检查：通过，`SYNTAX_OK 22 files`

## 阶段 3 补强：字段级知识接线与 query-aware 列保留
日期：2026-04-25

本阶段完成内容：
- 让 `column_semantics` 条目可以单独检索，不再只停留在表级知识召回。
- 在 `select_schema_context()` 中新增字段级知识检索，并将命中的字段知识转换成列优先级。
- 列裁剪升级为 query-aware 版本：优先保留主键、关系列、query 命中列和字段知识命中的列，再用原始列顺序补满预算。
- 将字段级知识提示补进 schema prompt。
- 在 trace / `review_debug` 中新增字段选择相关调试信息。
- 为列保留结果增加独立 metrics。

本阶段修改文件：
- `DataAnalyze/tools/knowledge_retrieval.py`
  新增字段级知识元信息、按 `kind` 检索和列提示收集逻辑。
- `DataAnalyze/tools/schema_selection.py`
  新增列优先级构建与 query-aware 列裁剪逻辑。
- `DataAnalyze/tools/db_tool.py`
  将字段级知识检索接入 schema 选择主链路。
- `DataAnalyze/schemas/models.py`
  在 `SchemaSelectionResult` 中新增字段选择相关字段。
- `DataAnalyze/agents/executor.py`
  将字段选择调试信息写入 `select_schema_context` trace。
- `DataAnalyze/agents/reviewer.py`
  将字段选择调试信息写入 `review_debug`。
- `DataAnalyze/middleware/metrics.py`
  新增列保留相关 metrics。
- `tests/test_knowledge_retrieval.py`
  新增字段级知识检索与字段保留测试。
- `DataAnalyze/README.md`
  同步当前能力说明。

行为说明：
- 当前列裁剪已经不再是纯“前 N 列截断”。
- 如果 query 命中字段名、字段语义或字段知识条目，对应列会优先保留进 prompt。
- 这一步没有改变 SQL 只读护栏，也没有改变 `/chat` 返回结构，只增加了可选调试字段。

新增观测字段：
- metrics
  - `dataanalyze_schema_selected_columns`
- `review_debug`
  - `column_selection_strategy`
  - `selected_columns_by_table`
  - `knowledge_column_hints`

验证证据：
- `python -m unittest tests.test_knowledge_retrieval`：通过，`Ran 6 tests ... OK`
- `python -m unittest tests.test_schema_metadata`：通过，`Ran 13 tests ... OK`
- `python -m unittest discover -s tests -p "test_*.py"`：通过，`Ran 29 tests ... OK`
- 使用 `compile(...)` 对 `DataAnalyze` 和 `tests` 做语法检查：通过，`SYNTAX_OK 22 files`

## 阶段 3 补强：docs 全量同步与受约束的 LLM 字段 Planner
日期：2026-04-25

本阶段完成内容：
- 将 `docs/project_evolution/` 下的活文档统一同步到当前代码状态。
- 同步更新 `README.md`，避免仓库内出现两套说法。
- 在候选表详情加载之后、最终列裁剪之前接入“受约束的 LLM 字段 planner”。
- planner 输入包含：
  - 用户 query
  - 候选表
  - 局部详细 schema
  - `column_semantics`
  - `query_pattern`
  - `metric_definition`
- planner 输出固定为：
  - `required_columns_by_table`
  - `optional_columns_by_table`
  - `reason`
- 系统约束层负责：
  - 过滤不存在字段
  - 过滤非候选表字段
  - 自动补主键、关系列、时间列
  - 在现有列预算内收敛
- planner 不可用或输出无效时，自动回退到现有 query-aware 列保留逻辑。
- 在 `SchemaSelectionResult`、`tool_calls`、`review_debug` 和 metrics 中补齐 planner 相关观测字段。

本阶段修改文件：
- `DataAnalyze/tools/field_planner.py`
  新增字段 planner 纯逻辑 helper。
- `DataAnalyze/tools/db_tool.py`
  接入受约束的 LLM 字段 planner 主链路与回退逻辑。
- `DataAnalyze/schemas/models.py`
  在 `SchemaSelectionResult` 中补充 planner 结果字段。
- `DataAnalyze/middleware/metrics.py`
  新增字段 planner 观测指标。
- `DataAnalyze/agents/executor.py`
  将 planner 调试字段写入 `select_schema_context` trace。
- `DataAnalyze/agents/reviewer.py`
  将 planner 调试字段透传到 `review_debug`。
- `tests/test_field_planner.py`
  新增字段 planner 成功、回退与 `review_debug` 透传测试。
- `DataAnalyze/docs/project_evolution/README.md`
- `DataAnalyze/docs/project_evolution/01_需求基线.md`
- `DataAnalyze/docs/project_evolution/02_系统现状与主链路.md`
- `DataAnalyze/docs/project_evolution/03_演进路线图.md`
- `DataAnalyze/docs/project_evolution/04_架构决策记录.md`
- `DataAnalyze/docs/project_evolution/05_近期行动清单.md`
- `DataAnalyze/README.md`

行为说明：
- 这一步没有修改 SQL 只读护栏。
- 这一步没有破坏 `/chat` 既有响应结构，只新增了可选调试字段。
- planner 的角色是“在系统边界内给字段选择建议”，不是完全替代现有规则层。

阶段遗留：
- `query_pattern` / `metric_definition` 目前只进入字段 planner，还没有完整进入 SQL planner。
- 空结果与时间窗口策略仍需要继续优化。

## 阶段 3 补强：两阶段 Query Planner
日期：2026-04-25

本阶段完成内容：
- 新增第一阶段意图级 query planner，并接入 `select_schema_context()` 主链路。
- query planner 运行在轻量表发现之后、详细 schema 加载之前。
- planner 会规划：
  - `query_type`
  - `primary_metric`
  - `time_requirement`
  - `analysis_dimensions`
  - `filter_dimensions`
  - `candidate_tables_hard`
  - `candidate_tables_soft`
  - `join_needed`
- planner 输出会直接影响：
  - `detail_tables`
  - 最终候选表种子顺序
  - 第二阶段字段 planner 的输入摘要
- 现有字段 planner 保留，但职责调整为“在详细 schema 范围内做精确字段落地”。
- query-aware 规则层继续保留，作为 planner 失败时的稳定 fallback。
- 补齐 query planner 的 `review_debug`、trace 和 metrics。

本阶段修改文件：
- `DataAnalyze/tools/query_planner.py`
  新增第一阶段 query planner helper。
- `DataAnalyze/tools/db_tool.py`
  接入两阶段 planner 主链路。
- `DataAnalyze/tools/field_planner.py`
  接收第一阶段 planner 摘要作为输入。
- `DataAnalyze/schemas/models.py`
  在 `SchemaSelectionResult` 中补充 query planner 字段。
- `DataAnalyze/middleware/metrics.py`
  新增 query planner 指标。
- `DataAnalyze/agents/executor.py`
  在 trace 中透传 query planner 结果。
- `DataAnalyze/agents/reviewer.py`
  在 `review_debug` 中透传 query planner 结果。
- `tests/test_query_planner.py`
  新增 query planner 成功、回退和 debug 透传测试。
- `tests/test_field_planner.py`
  适配新的上游 query planner。
- `DataAnalyze/README.md`
- `DataAnalyze/docs/project_evolution/01_需求基线.md`
- `DataAnalyze/docs/project_evolution/02_系统现状与主链路.md`
- `DataAnalyze/docs/project_evolution/03_演进路线图.md`
- `DataAnalyze/docs/project_evolution/04_架构决策记录.md`
- `DataAnalyze/docs/project_evolution/05_近期行动清单.md`

行为说明：
- 这一步没有修改 SQL 只读护栏。
- 这一步没有破坏 `/chat` 既有响应结构，只新增了可选调试字段。
- 这一步默认开启 query planner，但 planner 失败时会自动回退到原有规则缩圈逻辑。

验证证据：
- `python -m unittest tests.test_query_planner`：通过，`Ran 4 tests ... OK`
- `python -m unittest tests.test_field_planner`：通过，`Ran 3 tests ... OK`
- `python -m unittest discover -s tests -p "test_*.py"`：通过，`Ran 36 tests ... OK`
- 使用 `compile(...)` 对 `DataAnalyze` 和 `tests` 做语法检查：通过，`SYNTAX_OK 26 files`

阶段遗留：
- 当前 query planner 还是意图级 planner，不是完整 SQL planner。
- `query_pattern` / `metric_definition` 还没有直接进入 SQL 生成策略层。
- 空结果与时间窗口策略仍需继续优化。

## 阶段 3 补强：调试页瘦身与分析/评审分栏展示
日期：2026-04-25

本阶段完成内容：
- 重做 `/debug` 前端调试页布局，保留原有调试能力但减少噪音。
- 去掉 `Workflow` 时间线展示区。
- 去掉 `Session` 列表展示区。
- 将每次对话输出拆成独立区域展示：
  - 本轮摘要
  - LLM 分析
  - 评审结果
  - Chart 渲染
  - 原始响应
  - Schema / Memory
- 评审区域不再一股脑只看原始 JSON，而是优先展示关键调试字段。

本阶段修改文件：
- `DataAnalyze/web/index.html`
  重做调试页布局与渲染逻辑。
- `DataAnalyze/README.md`
  同步新的调试页说明。

行为说明：
- 这一步只改前端调试页展示方式，不改 `/chat` 接口结构。
- 这一步没有删除后端 `workflow_events`、`/sessions` 等接口，先只从页面上去掉，避免误伤现有调试链路和监控。

阶段遗留：
- 当前后端仍保留历史 debug / metrics 字段，后面如果确认确实不再使用，再做一次保守清理会更稳。
## 2026-05-14：Perfetto/output.pb MVP 链路

### 背景

新增目标：让 DataAnalyze/agent 能面向 Perfetto trace 文件 `output.pb` 完成“性能问题 -> SQL -> 指标 -> 结论”的闭环。

### 本阶段完成

- 新增 `DataAnalyze/tools/perfetto_tool.py`。
- 默认读取仓库根目录 `output.pb`。
- 通过 Perfetto Trace Processor 执行 SQL。
- 新增 Perfetto SQL 只读护栏。
- 新增 `POST /perfetto/query`，支持直接调试 Perfetto SQL。
- 新增 `POST /perfetto/analyze`，支持模板化性能分析。
- 新增结构化模型：
  - `PerfettoQueryRequest`
  - `PerfettoAnalyzeRequest`
  - `PerfettoMetric`
  - `PerfettoAnalyzeResponse`
- 已跑通长耗时 slice 分析，能输出 metrics、evidence、conclusion。

### 关键结论

- `/perfetto/query` 和 `/perfetto/analyze` 不依赖 PostgreSQL。
- 这条链路不依赖 Ollama/LLM。
- 当前先作为独立工具链存在，暂不并入 `/chat` 主链路。
- 后续 agent 接入应采用“数据源路由 + 模板候选 + 受限 SQL 改写 + Reviewer 审核”。

### 后续待办

- 扩充 Perfetto 模板库。
- 建立 `DataAnalyze/knowledge/perfetto/`。
- 增加 Perfetto Reviewer。
- 再考虑接入 `/chat` 或统一 agent 编排。

## 2026-05-14：Perfetto Agent v1 联调入口

### 背景

在 MVP 查询链路跑通后，需要接入 agent 形态，同时保留后续 trace 数据抽取入库、多 dataset 管理和前端稳定联调的演进空间。

### 本阶段完成

- 新增 `POST /perfetto/agent`。
- 新增 `PerfettoAgentRequest` / `PerfettoAgentResponse` / `PerfettoReviewResult`。
- 新增 `PerfettoAgent`，作为前端联调入口的轻量 agent facade。
- 新增 `PerfettoDataSource` 协议。
- 新增 `TraceProcessorPerfettoSource`，当前内部复用 `PerfettoTool` 查询 `output.pb`。
- 新增 `DatabasePerfettoSource` 占位，后续用于入库后的 trace 查询。
- 新增 `perfetto_templates.py`，将问题到 plan、plan 到 SQL、rows 到 metrics/conclusion 的逻辑从工具层拆出。
- 新增 `PerfettoReviewer`，当前做规则审核，不接 LLM。
- `/perfetto/query` 和 `/perfetto/analyze` 增加可读错误返回，减少裸 500。

### 验证

- `python -B -c "import DataAnalyze.main"` 通过。
- 固定 Perfetto SQL 查询成功。
- `/perfetto/agent` service 层验证 long slice 和 CPU time 均成功。
- SQL guard 错误可返回结构化 400 payload。

### 后续

- 增加 frame/jank、counter 趋势、调度延迟模板。
- 增加 dataset 管理接口。
- 将模板知识沉淀到 `DataAnalyze/knowledge/perfetto/`。
- 再评估是否接入 `/chat` 或统一 agent 编排。
## 2026-05-14：tools 目录职责拆分

本次整理把原来平铺在 `DataAnalyze/tools/` 下的工具按数据源拆分：

- `DataAnalyze/tools/db/`：数据库工具链，包含 `DatabaseTool`、PostgreSQL schema loader、schema selection、knowledge retrieval、query planner、field planner 等。
- `DataAnalyze/tools/perfetto/`：Perfetto 工具链，包含 `PerfettoTool`、Perfetto data source、Perfetto templates。
- `DataAnalyze/tools/llm_tool.py`：保持为根目录共享工具，供 executor、reviewer、memory 等复用。

同步修改：

- 更新 `main.py`、`agents/executor.py`、`agents/perfetto_agent.py` 和测试中的 import。
- 删除旧的 `tools/db_tool.py`、`tools/perfetto_tool.py` 等根目录平铺文件，避免后续误读旧路径。
- 为新的 Perfetto Agent/source/template/reviewer 代码补充注释，说明当前 v1 为什么使用模板 SQL、为什么保留数据源抽象、为什么 reviewer 暂不接 LLM。

验证：

- 已确认代码中不再存在 `DataAnalyze.tools.db_tool`、`DataAnalyze.tools.perfetto_tool` 等旧导入。
- 后续继续以 `python -B -c "import DataAnalyze.main"` 作为最小导入检查。
