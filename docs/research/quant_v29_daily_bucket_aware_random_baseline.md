# V29 daily bucket-aware flag randomized baseline

状态：研究对照；不调参；不改策略；不解除 production blocker。

- target_month: 2026-05
- trials: 500
- seed: 20260630
- production_ready: false

## v29_price_meta_longhorizon_guard

- diagnosis: beats_random_median_only_not_validated
- observed_delta: 0.009641
- random_median_delta: 0.006451
- random_mean_delta: 0.006541
- random_best_delta: 0.012182
- observed_percentile_vs_random: 0.9
- beats_random_median: true

## v29_price_meta_ultradefensive

- diagnosis: beats_random_median_only_not_validated
- observed_delta: 0.006491
- random_median_delta: 0.004415
- random_mean_delta: 0.00444
- random_best_delta: 0.007769
- observed_percentile_vs_random: 0.918
- beats_random_median: true

## Decision

这只是 bucket-aware randomized baseline。即使 observed timing 优于随机，也仍然不能进策略代码；下一步必须做 look-ahead、monthly、WF 验证。
