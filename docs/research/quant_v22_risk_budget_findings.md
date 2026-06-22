# V22 Risk-Budget Overlay Findings

Date: 2026-06-08

Status: rejected research experiment. Production remains blocked.

## Purpose

V19 diagnostics showed that the difficult 2022-03 to 2023-03 OOS fold was not
caused by the V18 analyst factor; it was mainly low stock exposure plus some
defensive overlay losses. V22 tested whether a lower trend/gold sleeve or lower
hedge budget could improve the current V18 stable-rating baseline without adding
new degrees of freedom.

## Evidence Input

- Stock features: `v21_archive_20260608_stock_alt_features.parquet`
- Crisis features: `v21_archive_20260608_crisis_asset_features.parquet`
- Archive manifest SHA256:
  `951be13854170102f2a37d02b0285ddd911759baadefbed45302b25143145974`
- Archive completeness: `archive_complete_for_builder=true`,
  `live_fallback_files=[]`
- Result file:
  `data/backtest_results/quant_logic_research_v17_alt_data_grid_v22_risk_budget_grid.json`

## Results

| Candidate | Sharpe | Ann Ret | Ann Vol | MDD | Recent Sharpe |
| --- | ---: | ---: | ---: | ---: | ---: |
| `v18_revision_stable_freq10` | 1.0319 | 11.60% | 8.82% | -13.59% | 0.8077 |
| `v22_stable_cash_heavy` | 1.0121 | 11.09% | 8.49% | -13.59% | 0.6313 |
| `v22_stable_low_hedge_cash` | 0.9935 | 11.31% | 8.86% | -13.59% | 0.6576 |
| `v22_stable_no_trend_overlay` | 0.9594 | 10.49% | 8.33% | -13.59% | 0.2765 |

## Decision

Reject all V22 risk-budget variants. Lowering or removing the defensive trend
sleeve reduces volatility, but it reduces return and recent Sharpe even more.
Lowering hedge budget also fails to improve risk-adjusted return.

This means the remaining production performance blocker is not solved by simple
defensive overlay tuning. The next alpha work should focus on genuinely new
predictive signals or a separately validated crisis-alpha sleeve, not more local
threshold tweaks.
