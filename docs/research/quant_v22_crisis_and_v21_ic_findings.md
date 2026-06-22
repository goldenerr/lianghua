# V22 Crisis Sleeve and V21 Archive IC Findings

Date: 2026-06-08

Status: rejected crisis sleeve; IC diagnostics confirm only one stable analyst
revision signal. Production remains blocked.

## Evidence Inputs

- Stock features: `v21_archive_20260608_stock_alt_features.parquet`
- Crisis features: `v21_archive_20260608_crisis_asset_features.parquet`
- Analyst ratings source:
  `data/alt_archives/20260608/analyst_expectations/analyst_ratings_cninfo.parquet`
- Archive manifest SHA256:
  `951be13854170102f2a37d02b0285ddd911759baadefbed45302b25143145974`
- Archive completeness: `archive_complete_for_builder=true`,
  `live_fallback_files=[]`

## Crisis Sleeve Grid

Result file:
`data/backtest_results/quant_logic_research_v17_alt_data_grid_v22_crisis_grid.json`

| Candidate | Sharpe | Ann Ret | Ann Vol | MDD | Avg Crisis Assets | Recent Sharpe |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `v18_revision_stable_freq10` | 1.0319 | 11.60% | 8.82% | -13.59% | 0.00 | 0.8077 |
| `v17_crisis_light` | 0.9880 | 9.72% | 7.30% | -13.26% | 0.31 | 0.7246 |
| `v17_crisis_heavy` | 0.9680 | 9.64% | 7.38% | -13.26% | 0.31 | 0.6146 |

Decision: reject the current crisis sleeve. It lowers MDD slightly but gives up
too much return and recent Sharpe. Current public futures/ETF proxy data is not
a production-grade independent crisis-alpha sleeve.

## V21 Archive-Bound IC Diagnostics

Result files:

- `data/backtest_results/quant_alt_factor_ic_v18_v21_archive_20260608.json`
- `data/backtest_results/quant_alt_factor_ic_v18_v21_archive_20260608.csv`

Findings:

- Qualified signal count: 1
- Only qualified factor: `analyst_rating_score_mean_60d` at 20-day horizon
- Rank IC: 0.017645
- t-stat: 7.8407
- Dates: 559
- This reproduces the prior V18 conclusion under the V21 archive-bound input
  path.

Most order-flow, fund-flow, northbound, margin and intraday fields have `n=0`
for IC evaluation because the current public-source snapshots do not provide a
long historical point-in-time panel. They are now archived correctly for future
use, but they cannot yet support production-grade historical alpha validation.

## Production Implication

The current best local research baseline remains below the production Sharpe
threshold. Portfolio-level risk-budget tweaks and the current crisis sleeve do
not solve the performance gap. To approach an industry-leading system, the next
required work is not more parameter tuning; it is acquiring or building long
point-in-time panels for genuinely new information sources:

- historical analyst consensus/revision provider data,
- broad historical order-flow/fund-flow/main-force data,
- minute-level liquidity/reversal/microstructure history across the target
  universe,
- borrow/short availability and futures execution data that can support a real
  market-neutral sleeve.
