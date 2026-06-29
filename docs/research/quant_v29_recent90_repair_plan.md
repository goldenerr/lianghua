# V29 recent-90 修复计划（非调参）

状态：研究诊断；不改变策略参数；不解除生产 blocker。

- candidate_count: 2
- global_next_actions: defensive_offset_quality_audit, drawdown_scaling_lag_audit, regime_trigger_timing_audit, stock_sleeve_decay_root_cause
- should_change_strategy_parameters_now: false

## v29_price_meta_longhorizon_guard

- recent_sharpe: -2.4941
- recent_total_return: -0.0356
- recent_mdd: -0.045
- stock_alpha_contribution: -0.036188
- crisis_contribution: 0.001783
- worst_month: 2026-05

### 下一步动作

1. **stock_sleeve_decay_root_cause**
   - why: recent losses are primarily from stock alpha sleeves, not costs or paper evidence plumbing
   - next_diagnostic: attribute May-2026 selected names by factor bucket / industry / market-cap and compare against historical winning months
   - do_not_do: do not flip factor signs or lower exposure globally before cross-sectional attribution proves the regime boundary
2. **regime_trigger_timing_audit**
   - why: worst month kept high stock exposure while risk-off was mostly inactive
   - next_diagnostic: trace risk-off inputs 20 trading days before 2026-05 drawdown and measure trigger delay vs loss start
   - do_not_do: do not simply tighten drawdown thresholds; that risks reacting after damage and overfitting May
3. **defensive_offset_quality_audit**
   - why: crisis/carry contribution was positive but too small to offset stock sleeve losses
   - next_diagnostic: test whether available defensive assets had positive returns in May-2026 and whether allocation arrived before or after the loss cluster
   - do_not_do: do not just increase crisis weight without proving the sleeve is a contemporaneous hedge
4. **drawdown_scaling_lag_audit**
   - why: drawdown scaling appears after losses, so it may protect capital but not prevent the initial regime loss
   - next_diagnostic: compare dates of first stock-sleeve loss cluster, first reduce_scale day and subsequent recovery path
   - do_not_do: do not convert the MDD floor into a terminal stop; previous tests showed terminal stops are harmful

## v29_price_meta_ultradefensive

- recent_sharpe: -2.67
- recent_total_return: -0.024
- recent_mdd: -0.031
- stock_alpha_contribution: -0.026327
- crisis_contribution: 0.002675
- worst_month: 2026-05

### 下一步动作

1. **stock_sleeve_decay_root_cause**
   - why: recent losses are primarily from stock alpha sleeves, not costs or paper evidence plumbing
   - next_diagnostic: attribute May-2026 selected names by factor bucket / industry / market-cap and compare against historical winning months
   - do_not_do: do not flip factor signs or lower exposure globally before cross-sectional attribution proves the regime boundary
2. **regime_trigger_timing_audit**
   - why: worst month kept high stock exposure while risk-off was mostly inactive
   - next_diagnostic: trace risk-off inputs 20 trading days before 2026-05 drawdown and measure trigger delay vs loss start
   - do_not_do: do not simply tighten drawdown thresholds; that risks reacting after damage and overfitting May
3. **defensive_offset_quality_audit**
   - why: crisis/carry contribution was positive but too small to offset stock sleeve losses
   - next_diagnostic: test whether available defensive assets had positive returns in May-2026 and whether allocation arrived before or after the loss cluster
   - do_not_do: do not just increase crisis weight without proving the sleeve is a contemporaneous hedge

