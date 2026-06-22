# V20 Alternative Data PIT Archive Evidence

Date: 2026-06-08

Status: local research archive evidence. Production trading remains blocked.

## Purpose

V17 connected order-flow and intraday sources, but several provider endpoints are
snapshots or rolling recent windows. Those sources cannot be used as historical
alpha unless the system stores each daily pull as point-in-time evidence. V20
adds a local archival layer with content hashes and manifest verification.

## Implementation

- Archive script: `scripts/archive_alt_data_snapshots.py`
- Shared archive path: `data/alt_archives/`
- Archive layout: `data/alt_archives/<archive_date>/<source>/...`
- Manifest: `data/alt_archives/<archive_date>/manifest.json`
- Manifest hash: `data/alt_archives/<archive_date>/manifest.sha256`
- Default archive sources for future runs:
  `analyst_expectations`, `flow_signals`, `hedge_assets`, `intraday`
- Verification command:
  `.venv/bin/python scripts/archive_alt_data_snapshots.py --archive-date 20260608 --verify`

The script copies existing fetched files only. It does not fetch external data.
By default it refuses to overwrite an archived file whose hash differs. An
overwrite requires an explicit `--overwrite`, which should be treated as an
audited repair action.

## 20260608 Archive

- Sources: `flow_signals`, `intraday`
- File count: 21
- Flow files: 13
- Intraday files: 8
- Manifest SHA256:
  `cc246b0e276564cebcb41418ce6f6739240b1d2a49f23ca92ea4463c3aebe205`
- Previous manifest hash: `null` because this is the first local archive in the
  chain.

Archived examples:

| Source | File |
| --- | --- |
| `flow_signals` | `big_deal_current.parquet` |
| `flow_signals` | `fund_flow_ranks.parquet` |
| `flow_signals` | `northbound_aggregate.parquet` |
| `flow_signals` | `northbound_holdings/600519.parquet` |
| `intraday` | `bars_5m/600519.parquet` |
| `intraday` | `features/600519.parquet` |
| `intraday` | `microstructure_features.parquet` |

## Verification Result

The archive verified successfully:

- Archive date: 20260608
- Files verified: 21
- Manifest hash:
  `cc246b0e276564cebcb41418ce6f6739240b1d2a49f23ca92ea4463c3aebe205`

## Archive-Backed Feature Build

`scripts/build_alt_data_features_v17.py` can now consume archived snapshot
sources instead of mutable provider output directories:

```bash
env \
  QUANT_ALT_FEATURES_USE_ARCHIVE=1 \
  QUANT_ALT_FEATURES_ARCHIVE_DATE=20260608 \
  QUANT_ALT_FEATURES_OUTPUT_PREFIX=v20_archive \
  .venv/bin/python scripts/build_alt_data_features_v17.py
```

Build evidence:

- Output prefix: `v20_archive`
- Stock features:
  `data/alt_features/v20_archive_stock_alt_features.parquet`
- Crisis-asset features:
  `data/alt_features/v20_archive_crisis_asset_features.parquet`
- Summary:
  `data/alt_features/v20_archive_alt_feature_summary.json`
- Stock rows: 43986
- Stock symbols: 1169
- Stock dates: 627
- Crisis-asset rows: 7540
- Archive mode: `true`
- Archive complete for builder: `false`
- Archive date: `20260608`
- Archive manifest SHA256:
  `cc246b0e276564cebcb41418ce6f6739240b1d2a49f23ca92ea4463c3aebe205`
- Required archived sources: `flow_signals`, `intraday`
- Archived files used:
  `flow_signals/big_deal_current.parquet`,
  `flow_signals/fund_flow_ranks.parquet`,
  `flow_signals/margin_details.parquet`,
  `flow_signals/northbound_holdings.parquet`,
  `intraday/microstructure_features.parquet`
- Live fallback files still disclosed in summary:
  `analyst_expectations/analyst_ratings_cninfo.parquet`,
  `analyst_expectations/profit_forecast_em.parquet`,
  `hedge_assets/hedge_crisis_assets.parquet`

When archive mode is enabled, `flow_signals` and `intraday` are treated as
snapshot sources that must exist in the requested archive. Missing files fail
closed instead of silently falling back to mutable live directories.

## Archive-Backed Research Consumption

`scripts/research_alt_data_v17.py` can now consume the archive-backed feature
files explicitly:

```bash
env \
  QUANT_V17_MODE=grid \
  QUANT_V17_CANDIDATES=v17_v16_comparable \
  QUANT_V17_STOCK_ALT_FEATURE_FILE=v20_archive_stock_alt_features.parquet \
  QUANT_V17_CRISIS_ALT_FEATURE_FILE=v20_archive_crisis_asset_features.parquet \
  QUANT_V17_OUTPUT_SUFFIX=v20_archive_probe \
  .venv/bin/python scripts/research_alt_data_v17.py
```

Probe result:

- Output:
  `data/backtest_results/quant_logic_research_v17_alt_data_grid_v20_archive_probe.json`
- Stock feature file:
  `v20_archive_stock_alt_features.parquet`
- Crisis feature file:
  `v20_archive_crisis_asset_features.parquet`
- Embedded summary file:
  `v20_archive_alt_feature_summary.json`
- Embedded archive date: `20260608`
- Embedded archive manifest SHA256:
  `cc246b0e276564cebcb41418ce6f6739240b1d2a49f23ca92ea4463c3aebe205`
- Candidate: `v17_v16_comparable`
- Full-sample Sharpe: 1.0328
- Full-sample MDD: -13.58%

This probe is not a new production candidate; it verifies that research reports
can carry the same archive evidence as the feature builder.

## V21 Full Local Archive Pipeline

The first 20260608 archive was intentionally narrow and covered only
`flow_signals` plus `intraday`. V21 extends the local archive path to all current
feature-builder inputs and adds a daily pipeline:

- Pipeline script: `scripts/run_alt_archive_daily_pipeline.py`
- Readiness gate: `scripts/validate_alt_archive_readiness.py`
- Full local archive command:

```bash
.venv/bin/python scripts/run_alt_archive_daily_pipeline.py \
  --archive-date 20260608 \
  --overwrite \
  --output-prefix v21_archive_20260608
```

Pipeline result:

- Archived files: 669
- Sources:
  `analyst_expectations`, `flow_signals`, `hedge_assets`, `intraday`
- Current manifest SHA256:
  `951be13854170102f2a37d02b0285ddd911759baadefbed45302b25143145974`
- Replaces partial manifest SHA256:
  `cc246b0e276564cebcb41418ce6f6739240b1d2a49f23ca92ea4463c3aebe205`
- Feature output prefix: `v21_archive_20260608`
- Stock feature rows: 43986
- Stock feature symbols: 1169
- Crisis-asset rows: 7540
- `archive_complete_for_builder`: `true`
- `live_fallback_files`: `[]`
- Pipeline report:
  `data/alt_features/v21_archive_20260608_pipeline_report.json`

The readiness gate now fails closed unless `archive_complete_for_builder=true`
and `live_fallback_files=[]`. V17 finalist/WF mode also enforces this by default
through `QUANT_V17_REQUIRE_ARCHIVE_COMPLETE=1`.
The production deployment readiness gate now also requires an external
`alt_archive_readiness` evidence reference, so local archive success alone
cannot release production.

V21 research consumption probe:

```bash
env \
  QUANT_V17_MODE=grid \
  QUANT_V17_CANDIDATES=v17_v16_comparable \
  QUANT_V17_STOCK_ALT_FEATURE_FILE=v21_archive_20260608_stock_alt_features.parquet \
  QUANT_V17_CRISIS_ALT_FEATURE_FILE=v21_archive_20260608_crisis_asset_features.parquet \
  QUANT_V17_OUTPUT_SUFFIX=v21_archive_probe \
  .venv/bin/python scripts/research_alt_data_v17.py
```

- Output:
  `data/backtest_results/quant_logic_research_v17_alt_data_grid_v21_archive_probe.json`
- Embedded manifest SHA256:
  `951be13854170102f2a37d02b0285ddd911759baadefbed45302b25143145974`
- Embedded `archive_complete_for_builder`: `true`
- Embedded `live_fallback_files`: `[]`
- Candidate: `v17_v16_comparable`
- Full-sample Sharpe: 1.0328
- Full-sample MDD: -13.58%

This is still not a new alpha improvement; it proves that a research report can
consume a full local archive without live fallback.

## Production Gaps

- This is a local research archive, not external WORM retention.
- Production still requires approved WORM storage, entitlement evidence and
  scheduled daily fetch/archive jobs.
- A single full local archive day is not enough for order-flow or intraday
  historical alpha. It only establishes the daily archival mechanism.
- Finalist/WF research is now blocked by default when the feature summary is not
  archive-complete, but production still needs the same gate wired into CI/CD and
  external readiness evidence.
- `alt_archive_readiness` is now part of the production deployment evidence
  checklist, but a local `data/alt_features/...pipeline_report.json` is still not
  enough. Production needs an external artifact/WORM reference, for example
  `artifact://...`, `ci-artifact://...`, `worm://...` or `s3-object-lock://...`.
- Future research should run scheduled daily archive-backed builds and then
  consume only hash-bound feature snapshots in finalist WF/OOS runs.
