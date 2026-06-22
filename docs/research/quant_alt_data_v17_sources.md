# V17 Alternative Data Sources and Evidence

Date: 2026-06-08

Status: research data layer added; production trading remains blocked until
point-in-time archives, paid/approved providers, execution availability and
position-provider evidence are complete.

## Added Sources

- Analyst consensus and revisions:
  `scripts/fetch_analyst_expectations.py`
- Northbound, fund-flow, big-deal and margin-flow signals:
  `scripts/fetch_flow_signals.py`
- Intraday liquidity, reversal and microstructure features:
  `scripts/fetch_intraday_microstructure.py`
- Hedge execution proxies and crisis-alpha assets:
  `scripts/fetch_hedge_crisis_assets.py`
- Daily feature-store builder:
  `scripts/build_alt_data_features_v17.py`

## Sample Evidence

- Analyst expectations:
  `data/analyst_expectations/fetch_summary.json`
  - EastMoney consensus snapshot: 2370 rows.
  - CNInfo dated ratings: 2024-01-01 to 2026-05-29, 104801
    project-universe feature rows after aggregation.
  - Failures: 0.
- Flow signals:
  `data/flow_signals/fetch_summary.json`
  - Northbound aggregate: 2683 rows.
  - Northbound per-stock sample: 600519, 000001, 300750; 4655 rows.
  - Fund-flow rank snapshots: 3-day and 5-day horizons; 10394 rows.
  - Big-deal current snapshot: 4850 rows.
  - Margin details for 2026-05-29: SSE 1981 rows, SZSE 2072 rows.
  - Failure: EastMoney main-fund endpoint disconnected once; treated as optional
    partial evidence rather than a production-stable source.
- Intraday:
  `data/intraday/fetch_summary.json`
  - 600519, 000001, 300750; 1970 five-minute bars each.
  - Derived daily feature rows: 129.
  - Failures: 0.
- Hedge and crisis assets:
  `data/hedge_assets/fetch_summary.json`
  - Futures: IF0, IH0, IC0, IM0 from 2024-01-02 to 2026-05-29.
  - ETFs: cash, bond/duration, gold, commodity, US/HK equity proxies.
  - Combined rows: 7540.
  - Failures: 0.
- V17 feature store:
  `data/alt_features/v17_alt_feature_summary.json`
  - Stock alternative-data features: 43986 rows, 1169 project-universe symbols,
    627 dates.
  - Crisis-asset features: 7540 rows, 13 assets.

## V17 Research Result

- Added `scripts/research_alt_data_v17.py`.
- Comparable V16 reproduction candidate:
  - Full sample: Sharpe 1.0328, MDD -13.58%.
  - Recent 2024+ segment: Sharpe 0.7498, MDD -7.46%.
- V17 alt/crisis candidate:
  - Full sample: Sharpe 1.0189, MDD -13.58%.
  - Recent 2024+ segment: Sharpe 0.6937, MDD -7.81%.
  - Average usable alt coverage at rebalance: 120.1 symbols; max 1196.
- Interpretation:
  - The data layer is now connected and point-in-time guarded.
  - The current V17 alpha construction is a negative increment versus the V16
    comparable baseline.
  - Do not run V17 finalist WF or open any production gate from this result.
  - Next research should redesign the revision/order-flow alpha itself, not
    merely increase its portfolio weight.

## V18 Follow-Up

- Added rolling analyst-revision diagnostics in
  `docs/research/quant_alt_factor_v18_findings.md`.
- V18 built `data/alt_features/v18_stock_alt_features.parquet` with 696418
  rows, 1196 symbols and 627 dates.
- The cleanest IC signal is `analyst_rating_score_mean_60d` at a 20-day horizon
  with rank IC 0.017645 and t-stat 7.8407; it is the only signal that remains
  qualified after train/test direction-stability gating.
- Analyst event-count signals are now explicitly unqualified because their
  train/test directions flip.
- The best V18 stable-rating candidate improved full-sample Sharpe from 1.0328
  to 1.0346 and recent 2024+ Sharpe from 0.7498 to 0.8318, while MDD stayed
  roughly flat at -13.59%.
- V18 stable-rating finalist WF has avg OOS Sharpe 0.7558 and decay 0.2983, but
  still fails the production Sharpe gate (`S=false`) and keeps one negative OOS
  fold.
- V18 remains research-only; no production gate should be opened.

## Point-In-Time Rules

- Safe historical sources by row date:
  CNInfo ratings, northbound holdings, margin details, and intraday archived
  bars/features.
- Snapshot-only sources until daily archival is established:
  EastMoney consensus snapshot, fund-flow ranks and big-deal current snapshot.
- Research code must never backfill a current snapshot into past dates. A
  snapshot is usable only from its own `asof_date` onward.
- The V17 builder defaults to filtering stock features to `data/stock_list.json`
  to prevent unintended universe drift.

## Production Gaps Still Open

- Analyst data needs a paid or approved PIT consensus provider for full
  historical estimate revisions and earnings revisions.
- Fund/order-flow snapshots need daily WORM-style archival before they can be
  trusted as historical alpha.
- Intraday data needs scheduled daily archival and data-quality checks; Sina's
  public endpoint exposes a rolling recent window, not a full minute history.
- Futures/ETF bars are research/execution proxies only. Production hedging still
  requires broker/exchange-backed account binding, margin, borrow/short
  availability, contract rollover calendar, position reconciliation and approval
  evidence.
- No production gate is opened by this data-layer work. V17 must still pass
  strict WF/OOS, cost, drawdown, capacity and reconciliation gates.
