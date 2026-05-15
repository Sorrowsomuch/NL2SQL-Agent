-- 多元化压测数据（PostgreSQL）
-- 目标：用于验证 SQL 生成、LLM 分析、Reviewer 评审质量与图表多样性
-- 使用方式：
--   psql -U admin -d jchatmind -f DataAnalyze/sql/ops_log_event_diverse_seed.sql

-- 1) 清理旧压测数据（仅删除带标签 seed=diverse 的记录）
DELETE FROM ops_log_event
WHERE tags ->> 'seed' = 'diverse';

-- 2) 基础噪声流量：6 个服务，最近 48 小时
INSERT INTO ops_log_event (ts, service, host, trace_id, level, error_code, message, latency_ms, tags)
SELECT
    NOW() - (g || ' minute')::interval,
    (ARRAY['gateway-service','order-service','payment-service','inventory-service','user-service','search-service'])[(g % 6) + 1],
    (ARRAY['node-a','node-b','node-c','node-d'])[(g % 4) + 1],
    md5(('trace-diverse-' || g)::text),
    CASE
        WHEN g % 41 = 0 THEN 'ERROR'
        WHEN g % 23 = 0 THEN 'WARN'
        ELSE 'INFO'
    END,
    CASE
        WHEN g % 97 = 0 THEN 'GW_TIMEOUT'
        WHEN g % 89 = 0 THEN 'PAY_5XX'
        WHEN g % 131 = 0 THEN 'ORD_CONFLICT'
        ELSE NULL
    END,
    CASE
        WHEN g % 97 = 0 THEN 'gateway timeout to upstream service'
        WHEN g % 89 = 0 THEN 'payment gateway returned 502/503 intermittently'
        WHEN g % 131 = 0 THEN 'order conflict detected by idempotency key'
        WHEN g % 23 = 0 THEN 'retry triggered due to transient network jitter'
        ELSE 'request completed'
    END,
    CASE
        WHEN g % 97 = 0 THEN 1800 + (g % 400)
        WHEN g % 89 = 0 THEN 1300 + (g % 500)
        WHEN g % 23 = 0 THEN 320 + (g % 120)
        ELSE 70 + (g % 80)
    END,
    jsonb_build_object('env','dev','region', (ARRAY['cn-east','cn-north','us-west'])[(g % 3) + 1], 'seed','diverse')
FROM generate_series(1, 3500) AS t(g);

-- 3) 支付链路事故窗口（高密度异常，最近 90~60 分钟）
INSERT INTO ops_log_event (ts, service, host, trace_id, level, error_code, message, latency_ms, tags)
SELECT
    NOW() - interval '90 minute' + (g || ' second')::interval,
    'payment-service',
    (ARRAY['pay-a','pay-b'])[(g % 2) + 1],
    md5(('trace-incident-pay-' || g)::text),
    CASE WHEN g % 9 = 0 THEN 'FATAL' ELSE 'ERROR' END,
    CASE WHEN g % 3 = 0 THEN 'PAY_TIMEOUT' ELSE 'PAY_GATEWAY_5XX' END,
    CASE WHEN g % 3 = 0 THEN 'payment timeout during capture stage' ELSE 'bank gateway unstable with 5xx spike' END,
    1400 + (g % 900),
    jsonb_build_object('env','dev','region','cn-east','seed','diverse','incident','payment-spike')
FROM generate_series(1, 800) AS t(g);

-- 4) 库存服务慢查询窗口（高延迟但非高错误）
INSERT INTO ops_log_event (ts, service, host, trace_id, level, error_code, message, latency_ms, tags)
SELECT
    NOW() - interval '30 minute' + (g || ' second')::interval,
    'inventory-service',
    'inv-a',
    md5(('trace-slow-inv-' || g)::text),
    CASE WHEN g % 50 = 0 THEN 'WARN' ELSE 'INFO' END,
    NULL,
    'inventory stock aggregation query is slow',
    700 + (g % 600),
    jsonb_build_object('env','dev','region','cn-north','seed','diverse','incident','slow-query')
FROM generate_series(1, 600) AS t(g);

-- 5) 用户服务错误码分散场景（用于 TopN 归因测试）
INSERT INTO ops_log_event (ts, service, host, trace_id, level, error_code, message, latency_ms, tags)
SELECT
    NOW() - interval '10 hour' + (g || ' minute')::interval,
    'user-service',
    'user-a',
    md5(('trace-user-err-' || g)::text),
    'ERROR',
    (ARRAY['USR_AUTH_FAIL','USR_NOT_FOUND','USR_TOKEN_EXPIRED','USR_RATE_LIMIT'])[(g % 4) + 1],
    (ARRAY[
      'authentication failed for credential mismatch',
      'user profile missing in shard',
      'token expired before refresh',
      'request throttled by rate limiter'
    ])[(g % 4) + 1],
    180 + (g % 220),
    jsonb_build_object('env','dev','region','us-west','seed','diverse','scenario','error-code-mix')
FROM generate_series(1, 520) AS t(g);

-- 6) 边界数据：空 error_code、超长 message、不同时区文本
INSERT INTO ops_log_event (ts, service, host, trace_id, level, error_code, message, latency_ms, tags)
VALUES
(NOW() - interval '15 minute', 'gateway-service', 'gw-z', md5('trace-boundary-1'), 'WARN', NULL, repeat('upstream jitter observed; ', 20), 450, '{"env":"dev","seed":"diverse","scenario":"long-message"}'),
(NOW() - interval '14 minute', 'search-service', 'search-z', md5('trace-boundary-2'), 'ERROR', '', 'index shard relocation timeout, timezone=UTC+8', 980, '{"env":"dev","seed":"diverse","scenario":"empty-error-code"}'),
(NOW() - interval '13 minute', 'order-service', 'ord-z', md5('trace-boundary-3'), 'INFO', NULL, 'normal order flow, timezone=UTC-5', 90, '{"env":"dev","seed":"diverse","scenario":"timezone-marker"}');

-- 7) 校验统计
-- SELECT count(*) FROM ops_log_event WHERE tags ->> 'seed' = 'diverse';
-- SELECT service, level, count(*) FROM ops_log_event WHERE tags ->> 'seed' = 'diverse' GROUP BY service, level ORDER BY service, level;
-- SELECT error_code, count(*) FROM ops_log_event WHERE tags ->> 'seed' = 'diverse' AND level IN ('ERROR','FATAL') GROUP BY error_code ORDER BY count(*) DESC;
