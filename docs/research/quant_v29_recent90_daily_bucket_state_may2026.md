# V29 recent-90 daily liquidity/score bucket state

状态：研究诊断；不调参；不改策略；不解除 production blocker。

- target_month: 2026-05
- production_ready: false
- selected_stock_row_count: 23562
- daily_bucket_state_count: 722

## v29_price_meta_longhorizon_guard

- diagnosis: daily_bucket_state_available
- bucket_day_count: 380
- flagged_bucket_day_count: 115
- bucket_type_summary: liquidity_bucket sum=-0.196156 flags=29, score_bucket sum=-0.196156 flags=44, sleeve sum=-0.196156 flags=42

## v29_price_meta_ultradefensive

- diagnosis: daily_bucket_state_available
- bucket_day_count: 342
- flagged_bucket_day_count: 114
- bucket_type_summary: sleeve sum=-0.155436 flags=42, liquidity_bucket sum=-0.155436 flags=28, score_bucket sum=-0.155436 flags=44

## Decision

该报告只补齐 daily liquidity/score bucket state。它不是策略变更；下一步才可做真正 bucket-aware lagged simulation。
