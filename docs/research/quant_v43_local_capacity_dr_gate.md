# V43 本地容量与容灾门禁

状态：本地容量/DR 基准已生成，生产仍阻塞。

## 结论

- `local_capacity_dr_gate_passes=true`
- `production_like=false`
- `production_ready=false`
- V42 影子模拟盘证据通过：false

## 本地容量指标

- 事件数：20000
- 模拟品种数：200
- 模拟策略数：20
- 事件吞吐：196096.7 / 秒
- 行情到策略 P99：0.00225 ms
- 订单事件吞吐：74720.5 / 秒
- 信号到网关 P99：0.017 ms
- 峰值 RSS：24.05 MB
- 审计链完整性：true

## 本地容灾指标

- 切换到备用区域：true
- 拒绝不健康区域：true
- 本地 RTO：3e-06 秒
- 报告 RPO：0.5 秒

## 本地阻塞项

- 无

## 生产阻塞项

- 本地容量/DR 报告不是类生产环境或外部证明
- 仍缺外部 capacity_benchmark 证据引用
- 仍缺外部 failover_dr_test 证据引用
- V42 影子模拟盘 90 天证据门禁未通过

## 说明

- 该报告只证明本地工程路径可跑通，不证明真实生产容量。
- 生产仍必须提供独立的类生产压测、监控截图或报告引用，以及多区域容灾演练引用。
- 本地结果不得填入生产 readiness gate 的 `capacity_benchmark` 或 `failover_dr_test`。
