# V23 Alpha/Data Readiness Findings

Date: 2026-06-09

Status: production blocked. V23 adds a machine-readable alpha readiness
scorecard; it does not approve trading or open any release gate.

## Evidence Inputs

- Feature archive summary:
  `data/alt_features/v21_archive_20260608_alt_feature_summary.json`
- IC diagnostics:
  `data/backtest_results/quant_alt_factor_ic_v18_v21_archive_20260608.json`
- IC detail CSV:
  `data/backtest_results/quant_alt_factor_ic_v18_v21_archive_20260608.csv`
- V22 risk-budget grid:
  `data/backtest_results/quant_logic_research_v17_alt_data_grid_v22_risk_budget_grid.json`
- V22 crisis-sleeve grid:
  `data/backtest_results/quant_logic_research_v17_alt_data_grid_v22_crisis_grid.json`
- V23 scorecard outputs:
  `data/backtest_results/quant_alpha_readiness_v23.json`
  and `data/backtest_results/quant_alpha_readiness_v23.csv`

## Archive State

- Archive date: `20260608`
- Manifest SHA256:
  `951be13854170102f2a37d02b0285ddd911759baadefbed45302b25143145974`
- `archive_complete_for_builder=true`
- `live_fallback_files=[]`
- Stock feature rows: 43,986
- Stock feature symbols: 1,169
- Stock feature dates: 627
- Crisis asset rows: 7,540

This closes the local no-live-fallback research path for current alternative
data inputs. It is still local research evidence, not external WORM or CI
artifact attestation.

## Scorecard Result

Production readiness: `false`

Best current local baseline:

| Label | Sharpe | Ann Ret | Ann Vol | MDD | Win | Recent Sharpe |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `v18_revision_stable_freq10` | 1.0319 | 11.60% | 8.82% | -13.59% | 47.60% | 0.8077 |

Production gate status:

- Sharpe gate failed: 1.0319 < 1.20
- MDD gate passed: -13.59% >= -15.00%
- Win-rate gate passed: 47.60% >= 40.00%
- Qualified alpha signal count failed: 1 < 5

## Category Readiness

| Category | Score | Class | Qualified | Best factor | Max dates | Max coverage |
| --- | ---: | --- | ---: | --- | ---: | ---: |
| `analyst_revision_rolling` | 77.11 | research_component_not_production | 1 | `analyst_rating_score_mean_60d` | 578 | 1187.30 |
| `analyst_revision` | 33.83 | rejected_unstable_or_leaky_signal | 0 | `analyst_upgrade_count` | 366 | 139.60 |
| `analyst_consensus` | 15.00 | data_gap_insufficient_pit_history | 0 | - | 0 | 0.00 |
| `fund_flow` | 15.00 | data_gap_insufficient_pit_history | 0 | - | 0 | 0.00 |
| `intraday_microstructure` | 15.00 | data_gap_insufficient_pit_history | 0 | - | 0 | 0.00 |
| `margin_short` | 15.00 | data_gap_insufficient_pit_history | 0 | - | 0 | 0.00 |
| `northbound` | 15.00 | data_gap_insufficient_pit_history | 0 | - | 0 | 0.00 |
| `order_flow` | 15.00 | data_gap_insufficient_pit_history | 0 | - | 0 | 0.00 |

Interpretation:

- Rolling analyst revision is useful enough to retain as a research component,
  but it is not production-grade alone.
- Dated analyst revision event signals contain apparent IC in places, but fail
  train/test stability and are rejected.
- Consensus, northbound, fund-flow, margin/short, order-flow and intraday
  sources are archived, but current historical point-in-time depth is too short
  for production alpha validation.

## Portfolio Experiment Decisions

- Risk-budget variants: rejected; no V22 risk-budget candidate improved the
  V18 stable baseline.
- Crisis-alpha sleeve variants: rejected; current futures/ETF proxy sleeve
  lowered return and recent Sharpe despite a slightly softer drawdown.

## Production Blockers

1. Best local Sharpe remains below the 1.20 production gate.
2. Only one qualified alpha signal exists; the production research threshold is
   at least five independent qualified signals.
3. PIT history/cross-section is insufficient for consensus, fund-flow,
   intraday, margin/short, northbound and order-flow categories.
4. Alternative-data archive evidence is local only; external WORM or CI
   artifact attestation is still required.
5. V22 risk-budget variants were rejected.
6. V22 crisis-alpha sleeve variants were rejected.
7. Required paper-trading and small-live acceptance evidence is absent.

## Next Required Work

- Stop broad overlay tuning until new PIT alpha panels exist.
- Build or procure long historical PIT panels for analyst consensus revisions,
  northbound/main-fund/order-flow, margin/borrow and intraday microstructure.
- Re-test alpha category IC before portfolio blending.
- Only after local alpha gates pass, connect external WORM evidence,
  broker/exchange-backed borrow and position providers, futures rollover
  execution evidence, and paper-trading acceptance.
