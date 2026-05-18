分析这份 trace 里 CPU 占用最高的线程，并判断这些线程是否同时存在长耗时 slice。请按进程名、线程名聚合，统计每个线程的 cpu_time_ms、长 slice 次数、最长 slice 耗时、平均 slice 耗时，只保留 cpu_time_ms 大于 20ms 或最长 slice 超过 16ms 的线程，按 cpu_time_ms 降序返回前 20 个，并给出可能的卡顿原因。

perfetto-complex-1