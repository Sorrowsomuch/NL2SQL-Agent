from __future__ import annotations

"""DataAnalyze 项目级 schema 作用域配置。"""

# 默认只关注本项目当前真实会用到的核心表。
# 这样即使数据库里混入了别的项目表，schema 选择时也会先被收敛到这里。
PROJECT_SCHEMA_TABLE_ALLOWLIST = [
    "chat_sessions",
    "chat_memories",
    "ops_log_event",
]

# 如有明确不想被选中的表，也可以放到这里。
PROJECT_SCHEMA_TABLE_DENYLIST: list[str] = []


# TODO(user): 当后面接入自己业务表时，优先在 allowlist 里补充。
# 例如：
# PROJECT_SCHEMA_TABLE_ALLOWLIST.extend(["order_header", "order_item", "customer_profile"])
