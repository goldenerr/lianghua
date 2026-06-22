# V19 OOS Regime Diagnostics

Date: 2026-06-08

Status: research-only evidence. Production trading remains blocked.

## Purpose

V18 found a small but real positive alternative-data increment from
`analyst_rating_score_mean_60d`, but the finalist still failed production gates
because full-sample Sharpe stayed below 1.2 and the F1 walk-forward fold was
negative. V19 diagnoses that persistent F1 failure and tests a simple bear
re-entry rule.

## Evidence Files

- Diagnostic script: `scripts/research_oos_regime_v19.py`
- V19 research logic: `scripts/research_alt_data_v17.py`
- V19 grid report: `data/backtest_results/quant_logic_research_v19_reentry_grid.json`
- V18 F1 diagnostic:
  `data/backtest_results/quant_logic_research_v19_oos_regime_diagnostics_v18_revision_stable_freq10_f1.json`
- V19 F1 diagnostic:
  `data/backtest_results/quant_logic_research_v19_oos_regime_diagnostics_v19_reentry_stable_freq10_f1.json`

## F1 Window

- Fold: F1
- OOS dates: 2022-03-08 to 2023-03-23
- Market equal-weight Sharpe: 0.5341
- Market equal-weight annual return: 14.34%
- Market equal-weight MDD: -21.63%

The market had a deep drawdown but multiple sharp rebounds. The existing regime
logic avoided much of the drawdown but was too slow to participate in rebound
months.

## Baseline F1 Diagnosis

Candidate: `v18_revision_stable_freq10`

- F1 Sharpe: -1.0059
- F1 annual return: -2.69%
- F1 MDD: -6.01%
- Correlation to equal-weight universe: 0.2622
- Bear rebalance ratio: 61.54%
- Average stock exposure: 18.07%
- Average total exposure: 43.51%
- Average alt coverage: 0.0 symbols

Key observations:

- V18 alternative-data signals do not explain the F1 failure because the fold
  has no alternative-data coverage.
- The strategy frequently had zero stock exposure during rebound months:
  May 2022, June 2022 and January 2023.
- Worst days were often overlay-driven when stock exposure was zero, for
  example 2022-03-10 and 2022-03-15.
- Overlay attribution confirms that 2022-03-10 and 2022-03-15 were both
  `cash=55% / gold=45%` days with zero stock exposure, so the losses were from
  the defensive gold overlay rather than stock selection.
- The selected stock sleeve later re-entered with high exposure in March 2023,
  when the market weakened again.

## V19 Re-Entry Test

Candidate: `v19_reentry_stable_freq10`

Rule:

- If the long-horizon regime remains bear but 20-day median momentum is at least
  2% and 20-day breadth is at least 55%, restore limited stock exposure to 45%.
- Treat the re-entry state as normal for router and overlay purposes.
- The rule uses only data available at the rebalance date.

Grid result:

| Candidate | Full Sharpe | Full MDD | Recent 2024+ Sharpe | Recent MDD | Bear rebalances | Cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `v18_revision_stable_freq10` | 1.0346 | -13.59% | 0.8318 | -7.94% | 259 | 0.1837 |
| `v19_reentry_stable_freq10` | 1.0342 | -17.45% | 0.7514 | -7.92% | 218 | 0.2119 |

F1 diagnostic:

| Candidate | F1 Sharpe | F1 MDD | Avg stock exposure | Avg total exposure |
| --- | ---: | ---: | ---: | ---: |
| `v18_revision_stable_freq10` | -1.0059 | -6.01% | 18.07% | 43.51% |
| `v19_reentry_stable_freq10` | -0.8032 | -5.07% | 21.53% | 45.16% |

## Decision

Reject `v19_reentry_stable_freq10`.

The re-entry rule improves the F1 fold slightly, but it worsens full-sample MDD
from -13.59% to -17.45%, increases costs and degrades recent 2024+ Sharpe. This
is not a production-quality fix. It is a classic symptom of patching one bad
fold while harming the global risk profile.

## Next Research Work

- Stop simple threshold tuning around F1; require a stronger independent signal
  before increasing exposure in rebound regimes.
- Build daily point-in-time archives for order-flow, northbound, margin and
  intraday microstructure so rebound/reversal signals can be tested historically.
- Add explicit overlay attribution and overlay asset holdings to future
  diagnostics, because F1 worst days include overlay-driven losses. The V19
  diagnostic now records overlay weights for rebalance rows and worst days.
- Keep `v18_revision_stable_freq10` as the current best research baseline, but
  do not open production gates.
