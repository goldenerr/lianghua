# V29 recent-90 signal-decay report

状态：研究诊断，不调参，不解除生产 blocker。

- global_diagnosis: losses_are_month_concentrated, recent_drag_is_low_percentile_vs_own_history, stock_alpha_sleeves_are_primary_recent_drag
- should_parameter_tune_now: false

## v29_price_meta_longhorizon_guard

- recent_total_return: -0.035622
- recent_mdd: -0.045032
- diagnosis: stock_alpha_sleeves_are_primary_recent_drag, recent_drag_is_low_percentile_vs_own_history, losses_are_month_concentrated

### Dominant negative sleeves

- `contrib_price_lowvol_reversal`: recent_sum=-0.013018, percentile=0.00222, worst_month=2026-05 (-0.011035), flags=recent_negative,bottom_quintile_vs_history,low_recent_hit_rate,loss_concentrated_in_worst_month
- `contrib_price_ic_diversified`: recent_sum=-0.009045, percentile=0.008437, worst_month=2026-05 (-0.006423), flags=recent_negative,bottom_quintile_vs_history,low_recent_hit_rate,loss_concentrated_in_worst_month
- `contrib_price_defensive_breadth`: recent_sum=-0.0073, percentile=0.004218, worst_month=2026-05 (-0.004558), flags=recent_negative,bottom_quintile_vs_history,low_recent_hit_rate,loss_concentrated_in_worst_month
- `contrib_price_ic_alpha`: recent_sum=-0.006825, percentile=0.013099, worst_month=2026-05 (-0.005171), flags=recent_negative,bottom_quintile_vs_history,loss_concentrated_in_worst_month

## v29_price_meta_ultradefensive

- recent_total_return: -0.024043
- recent_mdd: -0.03097
- diagnosis: stock_alpha_sleeves_are_primary_recent_drag, recent_drag_is_low_percentile_vs_own_history, losses_are_month_concentrated

### Dominant negative sleeves

- `contrib_price_ic_diversified`: recent_sum=-0.010157, percentile=0.002969, worst_month=2026-05 (-0.007579), flags=recent_negative,bottom_quintile_vs_history,low_recent_hit_rate,loss_concentrated_in_worst_month
- `contrib_price_lowvol_reversal`: recent_sum=-0.00837, percentile=0.002056, worst_month=2026-05 (-0.006904), flags=recent_negative,bottom_quintile_vs_history,low_recent_hit_rate,loss_concentrated_in_worst_month
- `contrib_price_defensive_breadth`: recent_sum=-0.0078, percentile=0.00137, worst_month=2026-05 (-0.004921), flags=recent_negative,bottom_quintile_vs_history,low_recent_hit_rate,loss_concentrated_in_worst_month
