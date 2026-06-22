# V32 长周期交易约束候选研究记录

## 结论

在 2006-01-01 至 2026-05-29 的 20 年窗口中，接入 V31 免费交易状态约束后，原有 V29 候选仍能通过生产最低门槛，但最初没有候选完全满足更严格的“行业领先”门槛。

本轮新增并固化了一个长周期导向候选：

```text
v29_price_meta_longhorizon_guard
```

它是当前推荐候选，因为它同时满足：

- 20 年 V31 交易状态约束回测通过行业领先门槛。
- 2017 以后近年 V31 交易状态约束回测通过行业领先门槛。
- 20 年 V31 交易状态约束 robustness gate 通过。

## 候选参数

```text
base_gross=0.60
bull_gross=0.87
neutral_gross=0.60
risk_off_gross=0.16
vol_target=0.115
dd_reduce_threshold=0.05
dd_reduce_scale=0.35
dd_stop_threshold=0.09
dd_stop_scale=0.04
crisis_fraction=0.75
carry_fraction=0.25
```

该参数来自 `longhorizon_guard` 方向的小范围扫描，目标不是最高收益，而是压住长期 MDD 并保持所有 WF fold 的 OOS Sharpe 为正。

## 20 年 V31 约束回测

输出：

- `data/backtest_results/quant_logic_research_v29_portfolio_layer_20y_v31_execution_constrained.json`
- `data/backtest_results/quant_logic_research_v29_portfolio_layer_20y_v31_execution_constrained.csv`

结果：

```text
full_sharpe=2.0285
annual_return=15.95%
annual_volatility=6.63%
max_drawdown=-8.33%
win_rate=56.52%
avg_oos_sharpe=2.4013
min_oos_sharpe=0.1980
sharpe_decay=-0.0720
```

5 个 WF fold：

| fold | OOS Sharpe | OOS MDD |
| ---: | ---: | ---: |
| 0 | 4.2026 | -2.53% |
| 1 | 2.6012 | -2.75% |
| 2 | 0.1980 | -4.09% |
| 3 | 2.3224 | -5.25% |
| 4 | 2.6825 | -8.33% |

## 2017 后 V31 约束回测

输出：

- `data/backtest_results/quant_logic_research_v29_portfolio_layer_v31_execution_constrained.json`
- `data/backtest_results/quant_logic_research_v29_portfolio_layer_v31_execution_constrained.csv`

结果：

```text
full_sharpe=2.2848
annual_return=20.69%
max_drawdown=-8.54%
avg_oos_sharpe=2.4653
min_oos_sharpe=0.0425
```

说明：

- 近年样本也通过行业领先门槛。
- 2023-2024 fold 仍是最弱窗口，但 OOS Sharpe 保持正值。

## 20 年 V31 约束鲁棒性

输出：

- `data/backtest_results/quant_v29_robustness_20y_v31_execution_constrained.json`
- `data/backtest_results/quant_v29_robustness_20y_v31_execution_constrained_cases.csv`

结果：

```text
robustness_gate_passes=true
case_count=21
base_full_sharpe=2.0285
base_oos_sharpe=2.4013
base_max_drawdown=-8.33%
base_min_oos_sharpe=0.1980
min_sensitivity_full_sharpe=1.9100
min_sensitivity_oos_sharpe=2.3113
min_sensitivity_drawdown=-10.10%
min_cost_stress_full_sharpe=1.8446
min_cost_stress_oos_sharpe=2.2090
recent_90_sharpe=0.6503
recent_252_sharpe=2.7493
```

解释：

- Base case 满足严格行业领先门槛。
- 参数扰动和成本压力下满足 robustness gate，但不保证每个压力 case 都仍满足行业领先门槛。
- 这说明候选具备生产前研究韧性，但不是“成本任意翻倍仍行业第一”的不现实假设。

## Readiness 变化

总 readiness 已刷新：

```text
recommended_candidate=v29_price_meta_longhorizon_guard
recommended_candidate_reason=selected by 20y V31 execution-constrained robustness gate
production_ready=false
```

Paper launch package 也已切换到该候选：

```text
candidate=v29_price_meta_longhorizon_guard
paper_launch_ready=false
production_ready=false
```

## 仍未解除的生产 blocker

这次推进只解决了“本地研究收益和交易状态约束真实性”的一部分问题，不能直接上生产。

仍阻塞：

- 外部 WORM archive evidence 未提供。
- Secret Manager evidence 未提供。
- Approval Service evidence 未提供。
- 真实 market data provider 与 entitlement evidence 未提供。
- Exchange-backed position provider evidence 未提供。
- signed plugin review evidence 未提供。
- production calendar evidence 未提供。
- capacity benchmark evidence 未提供。
- V30 真实 vendor PIT/security status/corporate action/tick-minute/order-fill replay evidence 未提供。
- 90 天 approved paper trading 未完成。

## 下一步

本地可以继续做：

- 对 `v29_price_meta_longhorizon_guard` 生成资金影响评估。
- 用 V31 交易状态约束继续跑容量和成本敏感性报告。
- 将 90 天 paper trading 启动包保持 fail-closed，等待真实外部 evidence refs。

外部必须补齐：

- paper-start 9 个 evidence refs。
- V30 production-data gate 的 5 类真实 vendor/broker 数据。
