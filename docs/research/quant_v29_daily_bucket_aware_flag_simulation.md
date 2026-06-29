# V29 daily liquidity/score bucket-aware flag simulation

状态：研究仿真；不调参；不改策略；不解除 production blocker。

- target_month: 2026-05
- production_ready: false
- date_flag_count: 40
- reduction_fraction: 0.5
- max_daily_delta_fraction: 0.25

## v29_price_meta_longhorizon_guard

- diagnosis: bucket_aware_improved_target_month
- baseline_compound_return: -0.027217
- simulated_compound_return: -0.017577
- simulated_minus_baseline: 0.009641
- flagged_day_count: 12 / 18
- net_attribution_delta: 0.009786

## v29_price_meta_ultradefensive

- diagnosis: bucket_aware_improved_target_month
- baseline_compound_return: -0.018951
- simulated_compound_return: -0.01246
- simulated_minus_baseline: 0.006491
- flagged_day_count: 12 / 18
- net_attribution_delta: 0.006561

## Decision

这只是 bucket-aware attribution-level simulation。下一步必须与 randomized bucket-aware baseline 比较，不能直接进入策略代码。
