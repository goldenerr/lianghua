# V29 bucket/sleeve stress flag research simulation

状态：研究仿真；不调参；不改策略；不解除 production blocker。

- target_month: 2026-05
- production_ready: false
- observation_days: 3
- reduction_fraction: 0.5
- lagged_inputs_only: true

## v29_price_meta_longhorizon_guard

- baseline_compound_return: -0.027217
- simulated_compound_return: -0.027152
- simulated_minus_baseline: 6.5e-05
- flagged_day_count: 7 / 18
- net_attribution_delta: 4.7e-05

## v29_price_meta_ultradefensive

- baseline_compound_return: -0.018951
- simulated_compound_return: -0.019002
- simulated_minus_baseline: -5.1e-05
- flagged_day_count: 8 / 18
- net_attribution_delta: -6.1e-05

## Decision

这只是 attribution-level lagged simulation。即使改善 May-2026，也不能直接改生产策略；下一步必须跑 WF、randomized flag baseline、look-ahead audit、monthly distribution checks。
