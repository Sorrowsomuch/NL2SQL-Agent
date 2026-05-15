-- 可选：用于验证 LLM 分析与图表输出的实例数据
-- 执行前请确认已创建 ops_log_event 表

INSERT INTO ops_log_event (ts, service, host, trace_id, level, error_code, message, latency_ms, tags)
VALUES
(NOW() - INTERVAL '50 minute', 'gateway-service', 'gw-01', 'trace-gw-001', 'INFO', NULL, 'request completed', 95, '{"env":"dev","region":"cn-east"}'),
(NOW() - INTERVAL '48 minute', 'order-service', 'ord-01', 'trace-ord-001', 'WARN', NULL, 'retry triggered for downstream', 380, '{"env":"dev","region":"cn-east"}'),
(NOW() - INTERVAL '45 minute', 'payment-service', 'pay-01', 'trace-pay-001', 'ERROR', 'PAY_TIMEOUT', 'payment timeout on bank gateway', 1860, '{"env":"dev","region":"cn-east"}'),
(NOW() - INTERVAL '44 minute', 'payment-service', 'pay-01', 'trace-pay-002', 'ERROR', 'PAY_TIMEOUT', 'payment failed due to timeout', 1750, '{"env":"dev","region":"cn-east"}'),
(NOW() - INTERVAL '43 minute', 'payment-service', 'pay-02', 'trace-pay-003', 'FATAL', 'PAY_GATEWAY_5XX', 'downstream bank 502', 2140, '{"env":"dev","region":"cn-east"}'),
(NOW() - INTERVAL '40 minute', 'inventory-service', 'inv-01', 'trace-inv-001', 'INFO', NULL, 'stock sync success', 120, '{"env":"dev","region":"cn-east"}'),
(NOW() - INTERVAL '35 minute', 'order-service', 'ord-02', 'trace-ord-002', 'ERROR', 'ORD_409', 'duplicate order request', 620, '{"env":"dev","region":"cn-east"}'),
(NOW() - INTERVAL '30 minute', 'gateway-service', 'gw-02', 'trace-gw-002', 'WARN', NULL, 'upstream latency increased', 540, '{"env":"dev","region":"cn-east"}'),
(NOW() - INTERVAL '28 minute', 'payment-service', 'pay-01', 'trace-pay-004', 'ERROR', 'PAY_TIMEOUT', 'payment timeout on settlement service', 1920, '{"env":"dev","region":"cn-east"}'),
(NOW() - INTERVAL '20 minute', 'payment-service', 'pay-03', 'trace-pay-005', 'ERROR', 'PAY_GATEWAY_5XX', 'gateway returned 503', 1680, '{"env":"dev","region":"cn-east"}'),
(NOW() - INTERVAL '12 minute', 'order-service', 'ord-03', 'trace-ord-003', 'INFO', NULL, 'order created', 140, '{"env":"dev","region":"cn-east"}'),
(NOW() - INTERVAL '5 minute', 'payment-service', 'pay-02', 'trace-pay-006', 'ERROR', 'PAY_TIMEOUT', 'timeout while capturing payment', 2010, '{"env":"dev","region":"cn-east"}');
