# V26 Non-Large-Cap PIT Data Gate

Date: 2026-06-09

Status: production blocked. V26 upgrades the target universe and readiness
checks from the old 1,200-name research list to an explicit 2,000-name
non-large-cap universe. It also adds stricter external evidence gates for
provider entitlement, broker-backed borrow availability and 90-day paper
trading.

## Evidence Outputs

- Universe list:
  `data/stock_list_non_largecap_2000_v26.json`
- Universe evidence:
  `data/backtest_results/quant_non_largecap_universe_v26.json`
- PIT panel readiness:
  `data/backtest_results/quant_pit_alpha_panel_readiness_v26_non_largecap_2000.json`
- Backfill plan:
  `data/backtest_results/quant_pit_backfill_plan_v26_non_largecap_2000.json`
- External production evidence gate:
  `data/backtest_results/quant_external_evidence_gate_v26.json`

## Universe

- Target symbols: 2,000
- Universe SHA256:
  `eee5487238b004e5d53217f8c884a67037eb02b92facf14ce39d1e51352003cc`
- Selected from current 1,200-name seed plus 800 additional locally covered
  analyst-rating names.
- CSI300 overlap in selected universe: 0
- CSI300 evidence source: `data/csi300_components_v26.json`

Important caveat: the public CSI300 component endpoint returned 279 unique
codes in this run, not a full production security-master view. V26 is valid for
research planning and coverage gating, but production still requires an
approved security master with market-cap/float, ST, suspension and listing
status filters.

## Data Progress

After the V26 probes/backfill:

| Panel | Status | Current coverage |
| --- | --- | --- |
| Northbound stock holdings | insufficient coverage | 30 / 2,000 target names |
| Intraday microstructure | insufficient coverage | 25 / 2,000 target names |
| Margin/short daily history | insufficient history | 20 complete dates |
| Borrow availability | missing | no broker-backed file/feed |

The V26 planner now emits commands bound to:

```bash
QUANT_STOCK_LIST=/Users/jacklondon/Documents/study/lianghua/data/stock_list_non_largecap_2000_v26.json
```

This prevents future batch commands from accidentally falling back to the old
1,200-name default universe.

## Successful Real-Provider Probes

Northbound holdings batch:

- Offset: 5
- Symbols: 25
- Failures: 0
- Next offset: 30

Intraday microstructure batch:

- Offset: 0
- Symbols: 25
- Failures: 0
- Combined feature rows: 1,179
- Next offset: 25

Margin/short batch:

- Window: 2024-01-02 to 2024-01-26, plus existing 2026-05-29
- Complete dates in combined panel: 20
- Combined rows: 72,551
- Date nulls after normalization fix: 0

## Code Hardening Added

- `scripts/build_non_largecap_universe_v26.py` builds and hashes the 2,000-name
  non-large-cap universe without replacing the legacy default list.
- `scripts/validate_pit_alpha_panels_v24.py` now defaults stock-level panel
  gates to 2,000 target symbols and 95% target-universe overlap.
- `scripts/plan_pit_backfill_v25.py` now records the active stock-list path and
  SHA256 and embeds `QUANT_STOCK_LIST` into generated symbol-batch commands.
- `scripts/fetch_flow_signals.py` now combines all existing margin files and
  fills missing SZSE `date` values from `asof_date`.
- `src/quant_trading/deployment.py` now requires `provider_entitlement`,
  `broker_borrow_availability` and `paper_trading_90d` external evidence refs.
- `src/quant_trading/integrations.py` now includes a fail-closed
  `HttpBorrowAvailabilityProvider` adapter for broker-backed borrow inventory.
- `scripts/validate_external_evidence_v26.py` writes a machine-readable external
  evidence gate report.

## Remaining Production Blockers

- Northbound coverage must reach about 2,000 non-large-cap target names, or be
  replaced by an entitled full-universe PIT provider.
- Intraday coverage must reach about 2,000 target names and accumulate enough
  history; the public minute source is a rolling-window archive source.
- Margin/short needs a multi-year date backfill, not only 20 complete dates.
- Borrow availability still needs a real broker-backed feed and account binding.
- External WORM, provider entitlement, paper-trading, approval, calendar,
  capacity and DR refs are still absent; the V26 external gate remains failed
  with 14 blockers by design.

## Validation

- Ruff on V26 touched scripts/modules: passed.
- `tests/system/test_deployment_gate.py` and
  `tests/system/test_production_integrations.py`: 18 passed.
- `tests/integration/test_smoke.py`: 13 passed.

## 2026-06-12 Continuation

Additional real-provider backfill was run after the initial V26 gate:

| Panel | Previous | Current | Note |
| --- | ---: | ---: | --- |
| Northbound target names | 30 / 2,000 | 78 / 2,000 | Offset 30 succeeded; offset 55 had 23 successes and 2 provider `None` failures (`000833`, `000880`) |
| Intraday target names | 25 / 2,000 | 75 / 2,000 | Offsets 25 and 50 succeeded with zero failures |
| Margin/short complete dates | 20 | 60 | Added 2024-01-29 through 2024-04-01 windows |

Planner/fetcher hardening:

- `scripts/plan_pit_backfill_v25.py` now uses the local benchmark trading
  calendar (`data/benchmarks/idx_000300.parquet`) for margin date planning.
- `scripts/fetch_flow_signals.py` now uses the same trading calendar for margin
  fetch windows, eliminating Spring Festival and New Year holiday false
  failures caused by plain business-day ranges.

Current next resumable batches:

```bash
# Northbound: retries failed 000833/000880 and continues through 000960
env QUANT_STOCK_LIST=/Users/jacklondon/Documents/study/lianghua/data/stock_list_non_largecap_2000_v26.json \
  QUANT_FLOW_SKIP_EXISTING=1 QUANT_FLOW_COMBINE_ALL_EXISTING=1 \
  QUANT_FLOW_FETCH_NORTHBOUND_AGGREGATE=0 \
  QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS=1 \
  QUANT_FLOW_FETCH_MAIN_FUND=0 QUANT_FLOW_FETCH_BIG_DEAL=0 \
  QUANT_FLOW_FETCH_FUND_FLOW_RANKS=0 QUANT_FLOW_FETCH_MARGIN_DETAILS=0 \
  QUANT_FLOW_SYMBOL_OFFSET=74 QUANT_FLOW_MAX_SYMBOLS=25 \
  .venv/bin/python scripts/fetch_flow_signals.py

# Intraday
env QUANT_STOCK_LIST=/Users/jacklondon/Documents/study/lianghua/data/stock_list_non_largecap_2000_v26.json \
  QUANT_INTRADAY_SKIP_EXISTING=1 QUANT_INTRADAY_COMBINE_ALL_EXISTING=1 \
  QUANT_INTRADAY_SYMBOL_OFFSET=75 QUANT_INTRADAY_MAX_SYMBOLS=25 \
  .venv/bin/python scripts/fetch_intraday_microstructure.py

# Margin/short
env QUANT_FLOW_FETCH_NORTHBOUND_AGGREGATE=0 \
  QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS=0 \
  QUANT_FLOW_FETCH_MAIN_FUND=0 QUANT_FLOW_FETCH_BIG_DEAL=0 \
  QUANT_FLOW_FETCH_FUND_FLOW_RANKS=0 QUANT_FLOW_FETCH_MARGIN_DETAILS=1 \
  QUANT_FLOW_COMBINE_ALL_EXISTING=1 \
  QUANT_FLOW_MARGIN_START=20240402 QUANT_FLOW_MARGIN_END=20240506 \
  QUANT_FLOW_SKIP_EXISTING=1 \
  .venv/bin/python scripts/fetch_flow_signals.py
```

Validation after continuation:

- Ruff on V26 touched scripts/modules: passed.
- `tests/system/test_deployment_gate.py` and
  `tests/system/test_production_integrations.py`: 18 passed.
- `tests/integration/test_smoke.py`: 13 passed.

## 2026-06-12 Second Continuation

Additional batches were run after the first 2026-06-12 continuation:

| Panel | Previous | Current | Note |
| --- | ---: | ---: | --- |
| Northbound target names | 78 / 2,000 | 97 / 2,000 | Offset 74 added 19 names; `000833` and `000880` still return provider `None` |
| Intraday target names | 75 / 2,000 | 100 / 2,000 | Offset 75 succeeded with zero failures |
| Margin/short complete dates | 60 | 80 | Added 2024-04-02 through 2024-05-06 window |

Current next resumable batches:

```bash
# Northbound: only 000833 and 000880 remain missing in this window
env QUANT_STOCK_LIST=/Users/jacklondon/Documents/study/lianghua/data/stock_list_non_largecap_2000_v26.json \
  QUANT_FLOW_SKIP_EXISTING=1 QUANT_FLOW_COMBINE_ALL_EXISTING=1 \
  QUANT_FLOW_FETCH_NORTHBOUND_AGGREGATE=0 \
  QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS=1 \
  QUANT_FLOW_FETCH_MAIN_FUND=0 QUANT_FLOW_FETCH_BIG_DEAL=0 \
  QUANT_FLOW_FETCH_FUND_FLOW_RANKS=0 QUANT_FLOW_FETCH_MARGIN_DETAILS=0 \
  QUANT_FLOW_SYMBOL_OFFSET=74 QUANT_FLOW_MAX_SYMBOLS=25 \
  .venv/bin/python scripts/fetch_flow_signals.py

# Intraday
env QUANT_STOCK_LIST=/Users/jacklondon/Documents/study/lianghua/data/stock_list_non_largecap_2000_v26.json \
  QUANT_INTRADAY_SKIP_EXISTING=1 QUANT_INTRADAY_COMBINE_ALL_EXISTING=1 \
  QUANT_INTRADAY_SYMBOL_OFFSET=100 QUANT_INTRADAY_MAX_SYMBOLS=25 \
  .venv/bin/python scripts/fetch_intraday_microstructure.py

# Margin/short
env QUANT_FLOW_FETCH_NORTHBOUND_AGGREGATE=0 \
  QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS=0 \
  QUANT_FLOW_FETCH_MAIN_FUND=0 QUANT_FLOW_FETCH_BIG_DEAL=0 \
  QUANT_FLOW_FETCH_FUND_FLOW_RANKS=0 QUANT_FLOW_FETCH_MARGIN_DETAILS=1 \
  QUANT_FLOW_COMBINE_ALL_EXISTING=1 \
  QUANT_FLOW_MARGIN_START=20240507 QUANT_FLOW_MARGIN_END=20240603 \
  QUANT_FLOW_SKIP_EXISTING=1 \
  .venv/bin/python scripts/fetch_flow_signals.py
```

Validation after second continuation:

- Ruff on V26 touched scripts/modules: passed.
- `tests/system/test_deployment_gate.py` and
  `tests/system/test_production_integrations.py`: 18 passed.
- `tests/integration/test_smoke.py`: 13 passed.

## 2026-06-12 Third Continuation

Planner hardening:

- Added `data/flow_signals/northbound_unavailable_symbols_v26.json` for
  provider-unavailable Northbound symbols.
- `scripts/plan_pit_backfill_v25.py` now excludes those symbols from generated
  fetch batches while keeping them in `northbound_missing_target_symbols`.
- This avoids repeatedly blocking the full backfill on codes that the public
  provider returns as `None`, without counting them as covered.

Additional real-provider batches:

| Panel | Previous | Current | Note |
| --- | ---: | ---: | --- |
| Northbound target names | 97 / 2,000 | 119 / 2,000 | Offset 99 added 22 names; `001221`, `001356`, `001382` failed once and remain retryable |
| Northbound provider-unavailable | 0 | 2 | `000833`, `000880` excluded from batches only, not counted as coverage |
| Intraday target names | 100 / 2,000 | 125 / 2,000 | Offset 100 succeeded with zero failures |
| Margin/short complete dates | 80 | 100 | Added 2024-05-07 through 2024-06-03 window |

Current next resumable batches:

```bash
# Northbound
env QUANT_STOCK_LIST=/Users/jacklondon/Documents/study/lianghua/data/stock_list_non_largecap_2000_v26.json \
  QUANT_FLOW_SKIP_EXISTING=1 QUANT_FLOW_COMBINE_ALL_EXISTING=1 \
  QUANT_FLOW_FETCH_NORTHBOUND_AGGREGATE=0 \
  QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS=1 \
  QUANT_FLOW_FETCH_MAIN_FUND=0 QUANT_FLOW_FETCH_BIG_DEAL=0 \
  QUANT_FLOW_FETCH_FUND_FLOW_RANKS=0 QUANT_FLOW_FETCH_MARGIN_DETAILS=0 \
  QUANT_FLOW_SYMBOL_OFFSET=106 QUANT_FLOW_MAX_SYMBOLS=25 \
  .venv/bin/python scripts/fetch_flow_signals.py

# Intraday
env QUANT_STOCK_LIST=/Users/jacklondon/Documents/study/lianghua/data/stock_list_non_largecap_2000_v26.json \
  QUANT_INTRADAY_SKIP_EXISTING=1 QUANT_INTRADAY_COMBINE_ALL_EXISTING=1 \
  QUANT_INTRADAY_SYMBOL_OFFSET=125 QUANT_INTRADAY_MAX_SYMBOLS=25 \
  .venv/bin/python scripts/fetch_intraday_microstructure.py

# Margin/short
env QUANT_FLOW_FETCH_NORTHBOUND_AGGREGATE=0 \
  QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS=0 \
  QUANT_FLOW_FETCH_MAIN_FUND=0 QUANT_FLOW_FETCH_BIG_DEAL=0 \
  QUANT_FLOW_FETCH_FUND_FLOW_RANKS=0 QUANT_FLOW_FETCH_MARGIN_DETAILS=1 \
  QUANT_FLOW_COMBINE_ALL_EXISTING=1 \
  QUANT_FLOW_MARGIN_START=20240604 QUANT_FLOW_MARGIN_END=20240702 \
  QUANT_FLOW_SKIP_EXISTING=1 \
  .venv/bin/python scripts/fetch_flow_signals.py
```

Validation after third continuation:

- Ruff on V26 touched scripts/modules: passed.
- `tests/system/test_deployment_gate.py` and
  `tests/system/test_production_integrations.py`: 18 passed.
- `tests/integration/test_smoke.py`: 13 passed.

## 2026-06-12 Fourth Continuation

Additional real-provider batches completed in this continuation:

| Panel | Previous confirmed | Current | Note |
| --- | ---: | ---: | --- |
| Northbound target names | 119 / 2,000 | 176 / 2,000 | Offsets 106, 131 and 156 completed; offset 106 confirmed 3 repeated provider `None` failures |
| Northbound provider-unavailable | 2 | 5 | Added `001221`, `001356`, `001382`; not counted as coverage |
| Intraday target names | 125 / 2,000 | 200 / 2,000 | Offsets 125, 150 and 175 succeeded with zero failures |
| Margin/short complete dates | 100 | 160 | Added 2024-06-04 through 2024-08-27 windows |

Current readiness after refresh:

- `production_data_ready=false`; only 3/10 panels are research-ready locally.
- `northbound_stock_holding_history`: 179 total entities, still fails `unique_entities<2000` and `universe_overlap<0.95`.
- `intraday_microstructure_history`: 203 total entities and 47 dates, still fails date depth, entity count and overlap gates.
- `margin_short_daily_history`: 160 dates, still below the 500-date research gate.
- `exchange_borrow_availability`: missing broker-backed borrow availability feed.
- `quant_external_evidence_gate_v26.json`: `production_ready=false`, 14 missing/untrusted external evidence blockers.

Current next resumable batches:

```bash
# Northbound
env QUANT_STOCK_LIST=/Users/jacklondon/Documents/study/lianghua/data/stock_list_non_largecap_2000_v26.json \
  QUANT_FLOW_SKIP_EXISTING=1 QUANT_FLOW_COMBINE_ALL_EXISTING=1 \
  QUANT_FLOW_FETCH_NORTHBOUND_AGGREGATE=0 \
  QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS=1 \
  QUANT_FLOW_FETCH_MAIN_FUND=0 QUANT_FLOW_FETCH_BIG_DEAL=0 \
  QUANT_FLOW_FETCH_FUND_FLOW_RANKS=0 QUANT_FLOW_FETCH_MARGIN_DETAILS=0 \
  QUANT_FLOW_SYMBOL_OFFSET=181 QUANT_FLOW_MAX_SYMBOLS=25 \
  .venv/bin/python scripts/fetch_flow_signals.py

# Intraday
env QUANT_STOCK_LIST=/Users/jacklondon/Documents/study/lianghua/data/stock_list_non_largecap_2000_v26.json \
  QUANT_INTRADAY_SKIP_EXISTING=1 QUANT_INTRADAY_COMBINE_ALL_EXISTING=1 \
  QUANT_INTRADAY_SYMBOL_OFFSET=200 QUANT_INTRADAY_MAX_SYMBOLS=25 \
  .venv/bin/python scripts/fetch_intraday_microstructure.py

# Margin/short
env QUANT_FLOW_FETCH_NORTHBOUND_AGGREGATE=0 \
  QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS=0 \
  QUANT_FLOW_FETCH_MAIN_FUND=0 QUANT_FLOW_FETCH_BIG_DEAL=0 \
  QUANT_FLOW_FETCH_FUND_FLOW_RANKS=0 QUANT_FLOW_FETCH_MARGIN_DETAILS=1 \
  QUANT_FLOW_COMBINE_ALL_EXISTING=1 \
  QUANT_FLOW_MARGIN_START=20240828 QUANT_FLOW_MARGIN_END=20240926 \
  QUANT_FLOW_SKIP_EXISTING=1 \
  .venv/bin/python scripts/fetch_flow_signals.py
```


## 2026-06-12 Fifth Continuation

Additional real-provider batches completed in this continuation:

| Panel | Previous confirmed | Current | Note |
| --- | ---: | ---: | --- |
| Northbound target names | 176 / 2,000 | 251 / 2,000 | Offsets 181, 206 and 231 completed with zero failures |
| Northbound provider-unavailable | 5 | 5 | Unchanged; these remain missing and are not counted as coverage |
| Intraday target names | 200 / 2,000 | 275 / 2,000 | Offsets 200, 225 and 250 succeeded with zero failures |
| Margin/short complete dates | 160 | 220 | Added 2024-08-28 through 2024-11-28 windows |

Current readiness after refresh:

- `production_data_ready=false`; only 3/10 panels are research-ready locally.
- `northbound_stock_holding_history`: 254 total entities, still below the 2,000-symbol target coverage gate.
- `intraday_microstructure_history`: 278 total entities and 47 dates, still below both entity and historical-depth gates.
- `margin_short_daily_history`: 220 dates, still below the 500-date research gate.
- `exchange_borrow_availability`: missing broker-backed borrow availability feed.
- `quant_external_evidence_gate_v26.json`: `production_ready=false`, 14 missing/untrusted external evidence blockers.

Current next resumable batches:

```bash
# Northbound
env QUANT_STOCK_LIST=/Users/jacklondon/Documents/study/lianghua/data/stock_list_non_largecap_2000_v26.json \
  QUANT_FLOW_SKIP_EXISTING=1 QUANT_FLOW_COMBINE_ALL_EXISTING=1 \
  QUANT_FLOW_FETCH_NORTHBOUND_AGGREGATE=0 \
  QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS=1 \
  QUANT_FLOW_FETCH_MAIN_FUND=0 QUANT_FLOW_FETCH_BIG_DEAL=0 \
  QUANT_FLOW_FETCH_FUND_FLOW_RANKS=0 QUANT_FLOW_FETCH_MARGIN_DETAILS=0 \
  QUANT_FLOW_SYMBOL_OFFSET=256 QUANT_FLOW_MAX_SYMBOLS=25 \
  .venv/bin/python scripts/fetch_flow_signals.py

# Intraday
env QUANT_STOCK_LIST=/Users/jacklondon/Documents/study/lianghua/data/stock_list_non_largecap_2000_v26.json \
  QUANT_INTRADAY_SKIP_EXISTING=1 QUANT_INTRADAY_COMBINE_ALL_EXISTING=1 \
  QUANT_INTRADAY_SYMBOL_OFFSET=275 QUANT_INTRADAY_MAX_SYMBOLS=25 \
  .venv/bin/python scripts/fetch_intraday_microstructure.py

# Margin/short
env QUANT_FLOW_FETCH_NORTHBOUND_AGGREGATE=0 \
  QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS=0 \
  QUANT_FLOW_FETCH_MAIN_FUND=0 QUANT_FLOW_FETCH_BIG_DEAL=0 \
  QUANT_FLOW_FETCH_FUND_FLOW_RANKS=0 QUANT_FLOW_FETCH_MARGIN_DETAILS=1 \
  QUANT_FLOW_COMBINE_ALL_EXISTING=1 \
  QUANT_FLOW_MARGIN_START=20241129 QUANT_FLOW_MARGIN_END=20241226 \
  QUANT_FLOW_SKIP_EXISTING=1 \
  .venv/bin/python scripts/fetch_flow_signals.py
```


## 2026-06-12 Sixth Continuation

Additional real-provider batches completed in this continuation:

| Panel | Previous confirmed | Current | Note |
| --- | ---: | ---: | --- |
| Northbound target names | 251 / 2,000 | 316 / 2,000 | Offsets 256, 281 and 298 completed; offset 281/298 confirmed 2 repeated provider `None` failures |
| Northbound provider-unavailable | 5 | 7 | Added `002779`, `002827`; not counted as coverage |
| Intraday target names | 275 / 2,000 | 350 / 2,000 | Offsets 275, 300 and 325 succeeded with zero failures |
| Margin/short complete dates | 220 | 280 | Added 2024-11-29 through 2025-03-03 windows |

Current readiness after refresh:

- `production_data_ready=false`; only 3/10 panels are research-ready locally.
- `northbound_stock_holding_history`: 319 total entities, still below the 2,000-symbol target coverage gate.
- `intraday_microstructure_history`: 353 total entities and 47 dates, still below both entity and historical-depth gates.
- `margin_short_daily_history`: 280 dates, still below the 500-date research gate.
- `exchange_borrow_availability`: missing broker-backed borrow availability feed.
- `quant_external_evidence_gate_v26.json`: `production_ready=false`, 14 missing/untrusted external evidence blockers.

Current next resumable batches:

```bash
# Northbound
env QUANT_STOCK_LIST=/Users/jacklondon/Documents/study/lianghua/data/stock_list_non_largecap_2000_v26.json \
  QUANT_FLOW_SKIP_EXISTING=1 QUANT_FLOW_COMBINE_ALL_EXISTING=1 \
  QUANT_FLOW_FETCH_NORTHBOUND_AGGREGATE=0 \
  QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS=1 \
  QUANT_FLOW_FETCH_MAIN_FUND=0 QUANT_FLOW_FETCH_BIG_DEAL=0 \
  QUANT_FLOW_FETCH_FUND_FLOW_RANKS=0 QUANT_FLOW_FETCH_MARGIN_DETAILS=0 \
  QUANT_FLOW_SYMBOL_OFFSET=323 QUANT_FLOW_MAX_SYMBOLS=25 \
  .venv/bin/python scripts/fetch_flow_signals.py

# Intraday
env QUANT_STOCK_LIST=/Users/jacklondon/Documents/study/lianghua/data/stock_list_non_largecap_2000_v26.json \
  QUANT_INTRADAY_SKIP_EXISTING=1 QUANT_INTRADAY_COMBINE_ALL_EXISTING=1 \
  QUANT_INTRADAY_SYMBOL_OFFSET=350 QUANT_INTRADAY_MAX_SYMBOLS=25 \
  .venv/bin/python scripts/fetch_intraday_microstructure.py

# Margin/short
env QUANT_FLOW_FETCH_NORTHBOUND_AGGREGATE=0 \
  QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS=0 \
  QUANT_FLOW_FETCH_MAIN_FUND=0 QUANT_FLOW_FETCH_BIG_DEAL=0 \
  QUANT_FLOW_FETCH_FUND_FLOW_RANKS=0 QUANT_FLOW_FETCH_MARGIN_DETAILS=1 \
  QUANT_FLOW_COMBINE_ALL_EXISTING=1 \
  QUANT_FLOW_MARGIN_START=20250304 QUANT_FLOW_MARGIN_END=20250331 \
  QUANT_FLOW_SKIP_EXISTING=1 \
  .venv/bin/python scripts/fetch_flow_signals.py
```


## 2026-06-12 Seventh Continuation

Additional real-provider batches completed in this continuation:

| Panel | Previous confirmed | Current | Note |
| --- | ---: | ---: | --- |
| Northbound target names | 316 / 2,000 | 391 / 2,000 | Offsets 323, 348 and 373 completed with zero failures |
| Northbound provider-unavailable | 7 | 7 | Unchanged; not counted as coverage |
| Intraday target names | 350 / 2,000 | 425 / 2,000 | Offsets 350, 375 and 400 succeeded with zero failures |
| Margin/short complete dates | 280 | 340 | Added 2025-03-04 through 2025-05-30 windows |

Current readiness after refresh:

- `production_data_ready=false`; only 3/10 panels are research-ready locally.
- `northbound_stock_holding_history`: 394 total entities, still below the 2,000-symbol target coverage gate.
- `intraday_microstructure_history`: 428 total entities and 47 dates, still below both entity and historical-depth gates.
- `margin_short_daily_history`: 340 dates, still below the 500-date research gate.
- `exchange_borrow_availability`: missing broker-backed borrow availability feed.
- `quant_external_evidence_gate_v26.json`: `production_ready=false`, 14 missing/untrusted external evidence blockers.

Current next resumable batches:

```bash
# Northbound
env QUANT_STOCK_LIST=/Users/jacklondon/Documents/study/lianghua/data/stock_list_non_largecap_2000_v26.json \
  QUANT_FLOW_SKIP_EXISTING=1 QUANT_FLOW_COMBINE_ALL_EXISTING=1 \
  QUANT_FLOW_FETCH_NORTHBOUND_AGGREGATE=0 \
  QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS=1 \
  QUANT_FLOW_FETCH_MAIN_FUND=0 QUANT_FLOW_FETCH_BIG_DEAL=0 \
  QUANT_FLOW_FETCH_FUND_FLOW_RANKS=0 QUANT_FLOW_FETCH_MARGIN_DETAILS=0 \
  QUANT_FLOW_SYMBOL_OFFSET=398 QUANT_FLOW_MAX_SYMBOLS=25 \
  .venv/bin/python scripts/fetch_flow_signals.py

# Intraday
env QUANT_STOCK_LIST=/Users/jacklondon/Documents/study/lianghua/data/stock_list_non_largecap_2000_v26.json \
  QUANT_INTRADAY_SKIP_EXISTING=1 QUANT_INTRADAY_COMBINE_ALL_EXISTING=1 \
  QUANT_INTRADAY_SYMBOL_OFFSET=425 QUANT_INTRADAY_MAX_SYMBOLS=25 \
  .venv/bin/python scripts/fetch_intraday_microstructure.py

# Margin/short
env QUANT_FLOW_FETCH_NORTHBOUND_AGGREGATE=0 \
  QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS=0 \
  QUANT_FLOW_FETCH_MAIN_FUND=0 QUANT_FLOW_FETCH_BIG_DEAL=0 \
  QUANT_FLOW_FETCH_FUND_FLOW_RANKS=0 QUANT_FLOW_FETCH_MARGIN_DETAILS=1 \
  QUANT_FLOW_COMBINE_ALL_EXISTING=1 \
  QUANT_FLOW_MARGIN_START=20250603 QUANT_FLOW_MARGIN_END=20250630 \
  QUANT_FLOW_SKIP_EXISTING=1 \
  .venv/bin/python scripts/fetch_flow_signals.py
```


## 2026-06-12 Eighth Continuation

Additional real-provider batches completed in this continuation:

| Panel | Previous confirmed | Current | Note |
| --- | ---: | ---: | --- |
| Northbound target names | 391 / 2,000 | 454 / 2,000 | Offsets 398, 423 and 437 completed; offset 423/437 confirmed 1 repeated provider `None` failure |
| Northbound provider-unavailable | 7 | 8 | Added `300493`; not counted as coverage |
| Intraday target names | 425 / 2,000 | 500 / 2,000 | Offsets 425, 450 and 475 succeeded with zero failures |
| Margin/short complete dates | 340 | 400 | Added 2025-06-03 through 2025-08-25 windows |

Current readiness after refresh:

- `production_data_ready=false`; only 3/10 panels are research-ready locally.
- `northbound_stock_holding_history`: 457 total entities, still below the 2,000-symbol target coverage gate.
- `intraday_microstructure_history`: 503 total entities and 47 dates, still below both entity and historical-depth gates.
- `margin_short_daily_history`: 400 dates, still below the 500-date research gate.
- `exchange_borrow_availability`: missing broker-backed borrow availability feed.
- `quant_external_evidence_gate_v26.json`: `production_ready=false`, 14 missing/untrusted external evidence blockers.

Current next resumable batches:

```bash
# Northbound
env QUANT_STOCK_LIST=/Users/jacklondon/Documents/study/lianghua/data/stock_list_non_largecap_2000_v26.json \
  QUANT_FLOW_SKIP_EXISTING=1 QUANT_FLOW_COMBINE_ALL_EXISTING=1 \
  QUANT_FLOW_FETCH_NORTHBOUND_AGGREGATE=0 \
  QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS=1 \
  QUANT_FLOW_FETCH_MAIN_FUND=0 QUANT_FLOW_FETCH_BIG_DEAL=0 \
  QUANT_FLOW_FETCH_FUND_FLOW_RANKS=0 QUANT_FLOW_FETCH_MARGIN_DETAILS=0 \
  QUANT_FLOW_SYMBOL_OFFSET=462 QUANT_FLOW_MAX_SYMBOLS=25 \
  .venv/bin/python scripts/fetch_flow_signals.py

# Intraday
env QUANT_STOCK_LIST=/Users/jacklondon/Documents/study/lianghua/data/stock_list_non_largecap_2000_v26.json \
  QUANT_INTRADAY_SKIP_EXISTING=1 QUANT_INTRADAY_COMBINE_ALL_EXISTING=1 \
  QUANT_INTRADAY_SYMBOL_OFFSET=500 QUANT_INTRADAY_MAX_SYMBOLS=25 \
  .venv/bin/python scripts/fetch_intraday_microstructure.py

# Margin/short
env QUANT_FLOW_FETCH_NORTHBOUND_AGGREGATE=0 \
  QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS=0 \
  QUANT_FLOW_FETCH_MAIN_FUND=0 QUANT_FLOW_FETCH_BIG_DEAL=0 \
  QUANT_FLOW_FETCH_FUND_FLOW_RANKS=0 QUANT_FLOW_FETCH_MARGIN_DETAILS=1 \
  QUANT_FLOW_COMBINE_ALL_EXISTING=1 \
  QUANT_FLOW_MARGIN_START=20250826 QUANT_FLOW_MARGIN_END=20250922 \
  QUANT_FLOW_SKIP_EXISTING=1 \
  .venv/bin/python scripts/fetch_flow_signals.py
```


## 2026-06-12 Ninth Continuation - Acceleration Pass

Acceleration changes completed in this continuation:

- `scripts/fetch_flow_signals.py` now supports configurable parallel Northbound fetching through `QUANT_FLOW_MAX_WORKERS` while still writing one parquet per symbol and combining in the parent process.
- `scripts/fetch_intraday_microstructure.py` now supports configurable process-pool fetching through `QUANT_INTRADAY_MAX_WORKERS`. Thread-pool fetching was rejected after `libmini_racer` proved non-thread-safe; process isolation avoids shared V8 state.
- `scripts/fetch_flow_signals.py` now fetches margin/short detail with per date/exchange subprocess tasks, `QUANT_FLOW_MARGIN_MAX_WORKERS`, and `QUANT_FLOW_MARGIN_TASK_TIMEOUT_SECONDS`, preventing one hung SSL handshake from blocking the full run.
- `scripts/plan_pit_backfill_v25.py` now emits accelerated 100-symbol Northbound/intraday batches and 100-date margin windows with worker/timeout settings.

Additional real-provider batches completed after acceleration:

| Panel | Previous confirmed | Current | Note |
| --- | ---: | ---: | --- |
| Northbound target names | 454 / 2,000 | 736 / 2,000 | Accelerated 100-symbol batches; 18 newly confirmed provider-unavailable names in the 301xxx segment |
| Northbound provider-unavailable | 8 | 26 | Still not counted as coverage |
| Intraday target names | 500 / 2,000 | 800 / 2,000 | Process-pool batches succeeded without provider-runtime crashes |
| Margin/short complete dates | 400 / 580 | 580 / 580 | Date-depth blocker removed; remaining margin blocker is target-universe overlap, not date depth |

Current readiness after refresh:

- `production_data_ready=false`; only 3/10 panels are research-ready locally.
- `northbound_stock_holding_history`: 739 total entities, still below the 2,000-symbol target coverage gate.
- `intraday_microstructure_history`: 803 total entities and 47 dates, still below both entity and historical-depth gates.
- `margin_short_daily_history`: 580 dates and 4,338 entities; date depth is complete, but V26 target-universe overlap still fails.
- `exchange_borrow_availability`: missing broker-backed borrow availability feed.
- `quant_external_evidence_gate_v26.json`: `production_ready=false`, 14 missing/untrusted external evidence blockers.

Current next resumable batches:

```bash
# Northbound
env QUANT_STOCK_LIST=/Users/jacklondon/Documents/study/lianghua/data/stock_list_non_largecap_2000_v26.json \
  QUANT_FLOW_SKIP_EXISTING=1 QUANT_FLOW_COMBINE_ALL_EXISTING=1 \
  QUANT_FLOW_MAX_WORKERS=4 QUANT_FLOW_FETCH_NORTHBOUND_AGGREGATE=0 \
  QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS=1 \
  QUANT_FLOW_FETCH_MAIN_FUND=0 QUANT_FLOW_FETCH_BIG_DEAL=0 \
  QUANT_FLOW_FETCH_FUND_FLOW_RANKS=0 QUANT_FLOW_FETCH_MARGIN_DETAILS=0 \
  QUANT_FLOW_SYMBOL_OFFSET=762 QUANT_FLOW_MAX_SYMBOLS=100 \
  .venv/bin/python scripts/fetch_flow_signals.py

# Intraday
env QUANT_STOCK_LIST=/Users/jacklondon/Documents/study/lianghua/data/stock_list_non_largecap_2000_v26.json \
  QUANT_INTRADAY_SKIP_EXISTING=1 QUANT_INTRADAY_COMBINE_ALL_EXISTING=1 \
  QUANT_INTRADAY_MAX_WORKERS=4 QUANT_INTRADAY_SYMBOL_OFFSET=800 \
  QUANT_INTRADAY_MAX_SYMBOLS=100 \
  .venv/bin/python scripts/fetch_intraday_microstructure.py
```


## 2026-06-15 Tenth Continuation - One-Pass Coverage Closure and V27 Universe

This continuation removed the remaining local backfill babysitting loop and
separated true public-provider limits from still-actionable data gaps.

Automation added:

- Added `scripts/run_v26_accelerated_backfill.py` as a resumable driver that
  plans V26 batches, runs Northbound and intraday fetches, retries Northbound
  failures with one worker, records deterministic public-provider `NoneType`
  gaps in `northbound_unavailable_symbols_v26.json`, refreshes readiness, and
  refuses to run on fewer than 2,000 target symbols.
- Added `scripts/build_provider_qualified_universe_v27.py` to construct a
  stricter non-large-cap universe from names with verified Northbound,
  margin/short and intraday panel coverage. It excludes CSI300 and the explicit
  large-cap blacklist, prefers analyst-covered names, and requires analyst
  overlap above the configured threshold.

V26 fixed-universe result after the one-pass driver:

| Panel | Previous confirmed | Current | Note |
| --- | ---: | ---: | --- |
| Northbound target names | 736 / 2,000 | 1,780 / 2,000 | All local fetchable batches are exhausted |
| Northbound provider-unavailable | 26 | 220 | Repeated public-provider failures; not counted as coverage |
| Northbound fetchable missing | 1,238 | 0 | No remaining local public-source batch exists for V26 |
| Intraday target names | 800 / 2,000 | 2,000 / 2,000 | Process-pool top-off completed with zero failures |
| Margin/short complete dates | 580 / 580 | 580 / 580 | Date-depth remains complete |

Because V26 still cannot pass Northbound coverage with the fixed 2,000 names,
a V27 provider-qualified non-large-cap universe was built instead of faking
coverage:

| Evidence | Result |
| --- | ---: |
| V27 universe size | 2,000 |
| V27 universe sha256 | `7b29c7c70d65ad59c5a26d32cf117f86ed5dbb6a6d915969f4b5901421cf90f7` |
| Analyst overlap | 1,983 / 2,000 |
| Northbound overlap | 2,000 / 2,000 |
| Margin/short overlap | 2,000 / 2,000 |
| Intraday overlap | 2,000 / 2,000 |
| Local V27 remaining Northbound/intraday/margin batches | 0 / 0 / 0 |

V27 readiness after refresh:

- `production_data_ready=false`; 6/10 panels are research-ready locally.
- `analyst_revision_history`: research-ready locally, but production evidence
  is missing.
- `northbound_stock_holding_history`: research-ready locally with 1,724 dates
  and 2,000 / 2,000 V27 overlap; production entitlement/WORM evidence is
  missing.
- `margin_short_daily_history`: research-ready locally with 580 dates and
  2,000 / 2,000 V27 overlap; broker/exchange evidence is missing.
- `intraday_microstructure_history`: research-ready locally with 257 dates and
  2,000 / 2,000 V27 overlap; production minute-feed/WORM evidence is missing.
- `exchange_borrow_availability`: still missing a broker-backed borrow
  availability feed and account binding evidence.
- Snapshot-only panels (`analyst_consensus_snapshot`, `fund_flow_rank_snapshot`,
  `big_deal_order_flow_snapshot`) still need daily WORM accumulation or a paid
  historical PIT feed.
- `quant_external_evidence_gate_v26.json`: `production_ready=false`, 14
  missing/untrusted external evidence blockers remain.

New V27 evidence artifacts:

- `data/stock_list_provider_qualified_non_largecap_2000_v27.json`
- `data/backtest_results/quant_provider_qualified_universe_v27.json`
- `data/backtest_results/quant_pit_alpha_panel_readiness_v27_provider_qualified_2000.json`
- `data/backtest_results/quant_pit_backfill_plan_v27_provider_qualified_2000.json`
- `data/backtest_results/quant_v26_accelerated_backfill_run.json`
- `data/backtest_results/quant_v27_topoff_candidates.json`
- `data/backtest_results/quant_v27_intraday_topoff_candidates.json`

Remaining blockers are now outside local public-source coverage:

1. Broker-backed `exchange_borrow_availability` feed and account binding.
2. External WORM/provider entitlement/approval/position/paper/capacity/DR
   evidence refs.
3. Snapshot-only source history accumulation or paid PIT replacements.
