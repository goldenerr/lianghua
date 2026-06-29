# V29 bucket stress flag randomized baseline

状态：研究对照；不调参；不改策略；不解除 production blocker。

- target_month: 2026-05
- trials: 500
- seed: 20260629
- production_ready: false

## v29_price_meta_longhorizon_guard

- diagnosis: does_not_beat_random_median
- observed_delta: 6.5e-05
- random_median_delta: 0.004299
- random_mean_delta: 0.004368
- random_best_delta: 0.016995
- observed_percentile_vs_random: 0.182
- beats_random_median: false

## v29_price_meta_ultradefensive

- diagnosis: no_positive_observed_edge
- observed_delta: -5.1e-05
- random_median_delta: 0.004632
- random_mean_delta: 0.00462
- random_best_delta: 0.013979
- observed_percentile_vs_random: 0.098
- beats_random_median: false

## Decision

这只是 randomized baseline。若 observed flag 打不过随机或优势极弱，则 sleeve-only flag 应继续视为被证伪，不能进入策略代码。
