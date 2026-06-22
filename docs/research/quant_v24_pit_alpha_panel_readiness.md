# V24 PIT Alpha Panel Readiness

Date: 2026-06-09

Status: production blocked. V24 validates whether the requested new
information sources are usable as point-in-time research panels. It does not
approve trading.

## Evidence Outputs

- JSON report:
  `data/backtest_results/quant_pit_alpha_panel_readiness_v24.json`
- CSV report:
  `data/backtest_results/quant_pit_alpha_panel_readiness_v24.csv`
- Validator:
  `scripts/validate_pit_alpha_panels_v24.py`
- Archive summary:
  `data/alt_features/v21_archive_20260608_alt_feature_summary.json`

Archive manifest SHA256:
`951be13854170102f2a37d02b0285ddd911759baadefbed45302b25143145974`

## Overall Result

- Total target panels: 10
- Research-ready local panels: 3
- Production-ready panels: 0
- Snapshot-only panels: 3
- Missing panels: 1
- Production data readiness: `false`

## Panel Status

| Panel | Status | Dates | Entities | Universe overlap | Research ready |
| --- | --- | ---: | ---: | ---: | --- |
| `analyst_revision_history` | research_ready_external_evidence_missing | 630 | 3,976 | 93.67% | yes |
| `northbound_market_aggregate_history` | research_ready_external_evidence_missing | 2,683 | 1 | - | yes |
| `hedge_crisis_asset_history` | research_ready_external_evidence_missing | 580 | 13 | - | yes |
| `analyst_consensus_snapshot` | snapshot_only_not_historical | 1 | 2,370 | 70.25% | no |
| `fund_flow_rank_snapshot` | snapshot_only_not_historical | 1 | 3,708 | 71.25% | no |
| `big_deal_order_flow_snapshot` | snapshot_only_not_historical | 1 | 571 | 15.33% | no |
| `northbound_stock_holding_history` | insufficient_history_or_coverage | 1,723 | 3 | 0.00% | no |
| `margin_short_daily_history` | insufficient_history_or_coverage | 1 | 4,053 | 95.42% | no |
| `intraday_microstructure_history` | insufficient_history_or_coverage | 43 | 3 | 0.00% | no |
| `exchange_borrow_availability` | missing | 0 | 0 | 0.00% | no |

## Interpretation

- Analyst revision history is the only broad stock-level PIT panel currently
  usable for research. V23 already showed it contributes one stable signal, but
  this is not enough to pass alpha production gates.
- Northbound market aggregate is a valid regime input, but it is not a
  stock-level alpha panel.
- Hedge/crisis asset history is usable for research, but live usage still
  requires broker/exchange-backed tradability, margin, rollover and position
  provider evidence.
- Analyst consensus, fund-flow and big-deal data are current snapshots. They
  need either paid historical PIT feeds or daily WORM snapshots over time.
- Northbound individual holdings contain long history only for three sampled
  names, and those names are not part of the current target universe.
- Margin/short data currently covers only one date. It needs a multi-year
  backfill and exchange-backed production evidence.
- Intraday microstructure has only 43 dates for three sampled names. This must
  become a long historical minute panel or a daily archived feature panel.
- Real borrow/short availability is absent. Without it, a genuine market-neutral
  or short sleeve remains blocked.

## Required Data Work

1. Expand northbound individual holdings beyond sample mode or procure full
   universe PIT holdings.
2. Accumulate daily WORM snapshots for fund-flow, big-deal and intraday data
   until the history is long enough for IC and walk-forward validation.
3. Backfill margin/short data over multiple years where public providers allow,
   and replace with exchange-backed feeds for production.
4. Add broker-backed borrow availability before designing real short or
   market-neutral sleeves.
5. Attach external WORM/provider entitlement evidence before any production
   deployment gate can pass.

## Incremental Backfill Controls

`scripts/fetch_flow_signals.py` now supports batch controls so full-universe
Northbound holdings can be expanded without repeatedly refetching every symbol:

- `QUANT_FLOW_SYMBOL_OFFSET`
- `QUANT_FLOW_MAX_SYMBOLS`; use `0` for all remaining symbols after the offset
- `QUANT_FLOW_COMBINE_ALL_EXISTING=1`; default, rebuilds the combined parquet
  from all existing per-symbol files
- `QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS`
- `QUANT_FLOW_FETCH_FUND_FLOW_RANKS`
- `QUANT_FLOW_FETCH_MARGIN_DETAILS`

Example research-only batch:

```bash
env QUANT_FLOW_SYMBOL_OFFSET=0 QUANT_FLOW_MAX_SYMBOLS=50 \
  QUANT_FLOW_FETCH_MAIN_FUND=0 QUANT_FLOW_FETCH_BIG_DEAL=0 \
  QUANT_FLOW_FETCH_FUND_FLOW_RANKS=0 QUANT_FLOW_FETCH_MARGIN_DETAILS=0 \
  .venv/bin/python scripts/fetch_flow_signals.py
```

`scripts/fetch_intraday_microstructure.py` has the same batching pattern:

- `QUANT_INTRADAY_SYMBOL_OFFSET`
- `QUANT_INTRADAY_MAX_SYMBOLS`; use `0` for all remaining symbols after the
  offset
- `QUANT_INTRADAY_COMBINE_ALL_EXISTING=1`; default, rebuilds the combined
  feature parquet from all existing per-symbol files

These controls prevent the combined feature files from being overwritten by
only the latest batch.
