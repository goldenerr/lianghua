# V18 Alternative Factor Diagnostics

Date: 2026-06-08

Status: research-only evidence. Production trading remains blocked.

## Purpose

V17 connected the requested alternative-data sources, but the first
alternative-data portfolio was a negative increment versus the V16 comparable
baseline. V18 isolates the analyst-revision data before any wider portfolio
promotion:

- Build rolling 20-day and 60-day analyst revision features from dated CNInfo
  records.
- Run point-in-time factor IC diagnostics with a one-trading-day signal lag.
- Re-test only minimal portfolio candidates using the V18 feature store.
- Reject any candidate that improves a subperiod while degrading full-sample
  production gates.

## Evidence Files

- Feature builder: `scripts/build_alt_revision_features_v18.py`
- IC diagnostics: `scripts/analyze_alt_factor_ic_v18.py`
- Portfolio research: `scripts/research_alt_data_v17.py`
- Feature store: `data/alt_features/v18_stock_alt_features.parquet`
- Feature summary: `data/alt_features/v18_alt_feature_summary.json`
- IC report: `data/backtest_results/quant_alt_factor_ic_v18.json`
- Portfolio report: `data/backtest_results/quant_logic_research_v17_alt_data_grid.json`

## Feature Store

The V18 stock feature store contains:

- Rows: 696,418
- Symbols: 1,196
- Dates: 627
- Rolling columns:
  - `analyst_event_count_20d`
  - `analyst_revision_sum_20d`
  - `analyst_upgrade_minus_downgrade_20d`
  - `analyst_rating_score_mean_20d`
  - `analyst_event_count_60d`
  - `analyst_revision_sum_60d`
  - `analyst_upgrade_minus_downgrade_60d`
  - `analyst_rating_score_mean_60d`

Point-in-time rule: rolling features use only historical `publish_date` records,
and the research layer applies a further one-trading-day signal lag.

## IC Diagnostics

After train/test direction-stability gating, only one rolling
analyst-revision factor qualifies for research use. Other statistically visible
signals remain observation-only because their train/test direction is unstable.

| Factor | Horizon | Rank IC | t-stat | Train IC | Test IC | Oriented top-bottom train/test | Qualified |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `analyst_rating_score_mean_60d` | 20d | 0.017645 | 7.8407 | 0.002564 | 0.044717 | 0.000149 / 0.009302 | Yes |
| `analyst_event_count_20d` | 20d | -0.017192 | -4.3041 | -0.043983 | 0.030898 | 0.018781 / -0.006304 | No |
| `analyst_event_count_60d` | 20d | -0.015742 | -3.1200 | -0.047487 | 0.041242 | 0.020572 / -0.008494 | No |
| `analyst_event_count_20d` | 10d | -0.012957 | -3.5245 | -0.029914 | 0.016030 | 0.009255 / -0.002059 | No |

Interpretation:

- `analyst_rating_score_mean_60d` is the only stable V18 signal because train
  and test IC agree with the full-period direction and the oriented
  top-bottom spread remains positive in both segments.
- Analyst event-count factors look statistically strong in full-period IC, but
  train/test direction flips. The IC script now marks them unqualified.
- Snapshot-only order-flow and intraday sources still do not have enough daily
  point-in-time history for strict IC qualification.

## Portfolio Isolation Test

The V18 portfolio candidates were run against the V16 comparable baseline using
the V18 feature file.

| Candidate | Full Sharpe | Full MDD | Recent 2024+ Sharpe | Recent 2024+ MDD | Decision |
| --- | ---: | ---: | ---: | ---: | --- |
| `v17_v16_comparable` | 1.0328 | -13.58% | 0.7498 | -7.46% | Baseline only |
| `v18_revision_stable_freq10` | 1.0346 | -13.59% | 0.8318 | -7.94% | Small positive increment, still fails Sharpe gate |
| `v18_revision_selected_freq10` | 1.0272 | -13.92% | 0.8520 | -7.68% | Recent improvement, full-sample degradation |
| `v18_revision_selected` | 0.8843 | -15.89% | 0.9028 | -9.43% | Reject, fails Sharpe and MDD gates |

The stable-rating-only 10-day candidate is the first V18 portfolio positive
increment: it improves full-sample Sharpe by 0.0018 and recent 2024+ Sharpe by
0.0820 versus the V16 comparable baseline. The improvement is too small to
change release status because the full-sample Sharpe remains far below the
production gate of 1.2.

## Stable Candidate Walk-Forward

`v18_revision_stable_freq10` finalist output:

- Report:
  `data/backtest_results/quant_logic_research_v17_v18_revision_stable_freq10_finalist.json`
- Full sample: Sharpe 1.0346, annual return 11.63%, MDD -13.59%, win rate
  47.63%.
- Recent 2024+ segment: Sharpe 0.8318, MDD -7.94%.
- WF average IS Sharpe: 1.0771.
- WF average OOS Sharpe: 0.7558.
- WF Sharpe decay: 0.2983.
- Gates: `S=false`, `M=true`, `D=true`, `W=true`, `C=true`, `A=false`.

Fold detail:

| Fold | IS Sharpe | OOS Sharpe | OOS MDD | Avg alt symbols OOS |
| --- | ---: | ---: | ---: | ---: |
| F0 | 1.1038 | 0.9736 | -6.81% | 0.0 |
| F1 | 1.0870 | -1.0059 | -6.01% | 0.0 |
| F2 | 1.0638 | 3.1941 | -2.36% | 305.4 |
| F3 | 1.0694 | 0.2695 | -8.89% | 1161.5 |
| F4 | 1.0614 | 0.3479 | -8.90% | 1183.8 |

Interpretation:

- The V18 stable rating factor helps most in OOS windows with actual alt-data
  coverage, especially F2.
- F1 remains sharply negative and has no V18 coverage, so the older baseline
  regime instability is still unsolved.
- Since full Sharpe is still below 1.2 and one OOS fold remains negative, this
  candidate is not production-ready.

## Decision

Do not open production gates for V18. The stable 60-day analyst rating signal is
now a valid research component for future regime-conditioned or short-horizon
sleeves, but the current portfolio remains below production standards.

## Next Research Work

- Build a stronger regime-conditioned analyst-revision sleeve around
  `analyst_rating_score_mean_60d`, with explicit handling for windows before
  alternative-data coverage begins.
- See `docs/research/quant_oos_regime_v19_findings.md` for the F1 OOS diagnosis
  and rejected bear re-entry experiment.
- Continue enforcing rolling train/test stability checks so full-period IC
  cannot hide direction flips.
- Archive daily order-flow, northbound, margin and intraday features before
  treating those categories as historical alpha.
- Obtain broker/exchange-backed short, futures, ETF borrow, margin and position
  evidence before any market-neutral or hedged sleeve can be promoted.
