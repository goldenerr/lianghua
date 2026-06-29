# V29 bucket/sleeve-aware risk flag hypothesis plan

状态：研究假设计划；不调参；不改策略；不解除 production blocker。

- target_month: 2026-05
- production_ready: false
- can_simulate_research_hypothesis_next: true

## v29_price_meta_longhorizon_guard

- hypothesis_type: sleeve_bucket_stress_flag
- hypothesis_ready_for_simulation: true
- can_implement_strategy_change_now: false
- timing_diagnosis: risk_off_missed_loss_window
- contrast_diagnosis: failed_buckets_flip_vs_positive_history
- top stress inputs: liquidity_bucket:low_amount_proxy score=0.147498, liquidity_bucket:mid_amount_proxy score=0.141759, score_bucket:selected_mid score=0.135056, score_bucket:selected_top score=0.123394, score_bucket:selected_low score=0.11414

## v29_price_meta_ultradefensive

- hypothesis_type: sleeve_bucket_stress_flag
- hypothesis_ready_for_simulation: true
- can_implement_strategy_change_now: false
- timing_diagnosis: risk_off_missed_loss_window
- contrast_diagnosis: failed_buckets_flip_vs_positive_history
- top stress inputs: liquidity_bucket:low_amount_proxy score=0.112414, sleeve:price_ic_diversified score=0.103408, score_bucket:selected_mid score=0.100885, liquidity_bucket:mid_amount_proxy score=0.100607, sleeve:price_lowvol_reversal score=0.098706

## Required validation before any strategy change

- walk_forward_oos_vs_unchanged_v29_baseline
- randomized_bucket_flag_baseline
- look_ahead_bias_audit_for_bucket_contribution_inputs
- monthly_distribution_check_including_2018_2020_2022_2026
- recent90_retest_and_worst_month_attribution
