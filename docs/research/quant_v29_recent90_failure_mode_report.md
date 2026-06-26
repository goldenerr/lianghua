# V29 recent-90 failure-mode report

状态：研究诊断，不调参，不解除生产 blocker。

- candidate_count: 2
- global_failure_modes: defensive_sleeve_under_offset, drawdown_scaling_active_after_damage, regime_detection_lag_or_under_de_risking, stock_alpha_sleeve_decay
- can_claim_industry_leading: false
- should_parameter_tune_now: false

## v29_price_meta_longhorizon_guard

- recent_total_return: -0.0356
- recent_sharpe: -2.4941
- recent_mdd: -0.045
- stock_alpha_contribution: -0.036188
- crisis_contribution: 0.001783
- failure_modes: stock_alpha_sleeve_decay, defensive_sleeve_under_offset, regime_detection_lag_or_under_de_risking, drawdown_scaling_active_after_damage
- worst_month: 2026-05

### Recommended next work

- Run signal-decay diagnostics by sleeve and month before any parameter changes.
- Audit risk-off trigger timing around the worst month; compare signal date vs drawdown start without changing thresholds.
- Evaluate whether crisis/carry sleeves are true offsets in the current A-share regime; do not simply increase weights.
- Check whether drawdown scaling reacts after losses rather than before them; keep it as diagnostics only.

## v29_price_meta_ultradefensive

- recent_total_return: -0.024
- recent_sharpe: -2.67
- recent_mdd: -0.031
- stock_alpha_contribution: -0.026327
- crisis_contribution: 0.002675
- failure_modes: stock_alpha_sleeve_decay, defensive_sleeve_under_offset, regime_detection_lag_or_under_de_risking
- worst_month: 2026-05

### Recommended next work

- Run signal-decay diagnostics by sleeve and month before any parameter changes.
- Audit risk-off trigger timing around the worst month; compare signal date vs drawdown start without changing thresholds.
- Evaluate whether crisis/carry sleeves are true offsets in the current A-share regime; do not simply increase weights.
