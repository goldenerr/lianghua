# V40 Paper-Shadow 启动手册

生成时间：2026-06-23T06:41:40.911262+00:00

## 状态

- `paper_shadow_ready=false`
- `production_ready=false`
- 该包只允许只读模拟盘影子验证，不允许实盘下单。

## 固定候选

- 资金：200000 元
- 股票预算上限：7.00%
- 危机预算上限：20.00%
- 股票调仓周期：40 日
- 危机资产：黄金 ETF 518880、国债 ETF 511010、十年国债 ETF 511260
- `auto_trade_enabled=false`
- `live_order_submission_allowed=false`
- `max_live_capital_fraction=0.0`

## 启动前阻塞项

- default_sensitivity_pass_ratio_passes
- stress_sensitivity_pass_ratio_passes
- stress_metric_spread_passes
- stress_cross_capital_passes
- production_minimum_passes
- approved external WORM archive evidence
- approved Secret Manager evidence
- approval-service flow and approval reference
- real market-data provider entitlement
- exchange-backed position provider evidence
- broker order/fill log archive path
- 90-day paper trading schedule and monitoring owner
- production calendar evidence
- capacity and disaster-recovery evidence

## 运行控制

- read-only market data subscription
- paper account only, no broker live-order permission
- all proposed orders written to audit bus before simulated fill
- daily cost/slippage report
- weekly backtest-vs-paper drift report
- manual approval before any change to candidate parameters

## 判断

该候选默认与高成本单点相对通过，但 V40 robustness 失败，不能进入生产。只有完成外部证据、90 天 paper shadow、滑点/成交偏差和周度回放对账后，才能重新评估是否进入更严格的 paper trading。
