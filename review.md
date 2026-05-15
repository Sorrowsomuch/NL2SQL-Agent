SELECT daily.service, daily.day, daily.error_count, avg30.avg_error_30d, ROUND(daily.error_count::numeric / NULLIF(avg30.avg_error_30d, 0), 2) AS exceed_ratio FROM (SELECT service, DATE(ts) AS day, COUNT(*) AS error_count FROM ops_log_event WHERE level = 'ERROR' AND ts >= CURRENT_DATE - INTERVAL '20 days' GROUP BY service, DATE(ts)) daily JOIN (SELECT service, COUNT(*)::numeric / 30 AS avg_error_30d FROM ops_log_event WHERE level = 'ERROR' AND ts >= CURRENT_DATE - INTERVAL '30 days' AND ts < CURRENT_DATE GROUP BY service) avg30 ON daily.service = avg30.service WHERE daily.error_count > 1.5 * avg30.avg_error_30d ORDER BY exceed_ratio DESC;"


客户端性能问题分析
性能问题原因
btrace 生成数据


