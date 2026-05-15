from __future__ import annotations

"""渐进式 schema 选择使用的轻量词表提示。"""

# 这里适合补充“业务词 -> 候选表”的直觉映射。
# 它不会绕过 SQL 只读护栏，也不会直接执行 SQL，只会帮助缩圈。
QUERY_TERM_HINTS = {
    "会话": ["chat_sessions", "chat_memories"],
    "session": ["chat_sessions", "chat_memories"],
    "记忆": ["chat_memories", "chat_sessions"],
    "memory": ["chat_memories", "chat_sessions"],
    "日志": ["ops_log_event"],
    "log": ["ops_log_event"],
    "错误": ["ops_log_event"],
    "error": ["ops_log_event"],
    "延迟": ["ops_log_event"],
    "latency": ["ops_log_event"],
}


# TODO(user): 在这里补业务同义词。
# 例如：
# QUERY_TERM_HINTS["订单"] = ["order_header", "order_item", "customer_profile"]
