-- DataAnalyze core tables (PostgreSQL)
-- 你可以先执行本文件建表，然后直接进行真实库联调。

CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id VARCHAR(128) PRIMARY KEY,
    title VARCHAR(255) DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS chat_memories (
    id BIGSERIAL PRIMARY KEY,
    session_id VARCHAR(128) NOT NULL,
    role VARCHAR(32) NOT NULL,
    memory_layer VARCHAR(32) NOT NULL,
    memory_type VARCHAR(32) NOT NULL,
    content TEXT NOT NULL,
    compressed BOOLEAN NOT NULL DEFAULT FALSE,
    salience_score NUMERIC(6,3) DEFAULT 0,
    source_range_start BIGINT,
    source_range_end BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_memories_session_created
    ON chat_memories (session_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_chat_memories_layer
    ON chat_memories (session_id, memory_layer, created_at DESC);

CREATE TABLE IF NOT EXISTS ops_log_event (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL,
    service VARCHAR(128) NOT NULL,
    host VARCHAR(128),
    trace_id VARCHAR(128),
    level VARCHAR(16) NOT NULL,
    error_code VARCHAR(64),
    message TEXT NOT NULL,
    latency_ms NUMERIC(12,2),
    tags JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ops_log_event_ts
    ON ops_log_event (ts DESC);

CREATE INDEX IF NOT EXISTS idx_ops_log_event_service_ts
    ON ops_log_event (service, ts DESC);

CREATE INDEX IF NOT EXISTS idx_ops_log_event_level_ts
    ON ops_log_event (level, ts DESC);

CREATE INDEX IF NOT EXISTS idx_ops_log_event_error_code_ts
    ON ops_log_event (error_code, ts DESC);

CREATE INDEX IF NOT EXISTS idx_ops_log_event_tags_gin
    ON ops_log_event USING GIN (tags);

-- Mock/联调用的初始化数据
INSERT INTO ops_log_event (ts, service, host, trace_id, level, error_code, message, latency_ms, tags)
VALUES
(NOW() - INTERVAL '5 minute', 'order-service', 'order-1', 'trace-001', 'ERROR', 'ORD-502', 'downstream timeout', 1260, '{"env":"dev"}'),
(NOW() - INTERVAL '4 minute', 'payment-service', 'payment-2', 'trace-002', 'ERROR', 'PAY-409', 'duplicate transaction', 920, '{"env":"dev"}'),
(NOW() - INTERVAL '3 minute', 'gateway', 'gateway-1', 'trace-003', 'INFO', NULL, 'request completed', 120, '{"env":"dev"}')
ON CONFLICT DO NOTHING;
