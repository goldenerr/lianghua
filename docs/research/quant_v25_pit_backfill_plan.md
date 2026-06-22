# V25 PIT Backfill Plan and Probe

Date: 2026-06-09

Status: production blocked. V25 turns the V24 data gaps into executable,
incremental backfill batches. It does not approve trading.

## Evidence Outputs

- Plan JSON:
  `data/backtest_results/quant_pit_backfill_plan_v25.json`
- Plan CSV:
  `data/backtest_results/quant_pit_backfill_plan_v25.csv`
- Probe JSON:
  `data/backtest_results/quant_pit_backfill_probe_v25.json`
- Planner:
  `scripts/plan_pit_backfill_v25.py`

## Plan Summary

- Target universe size: 1,200 symbols
- Northbound target symbols already present before full backfill: 5
- Northbound target symbols still missing: 1,195
- Intraday target symbols still missing: 1,200
- Complete margin/short dates currently present: 1
- Planned northbound batches: 48
- Planned intraday batches: 48
- Planned margin/short batches: 32

The default batch size is 25 target symbols for Northbound and intraday, and
20 business days for margin/short history.

## Successful Probe

A first research-only Northbound target-universe probe was run with:

```bash
env QUANT_FLOW_SYMBOL_OFFSET=0 QUANT_FLOW_MAX_SYMBOLS=5 \
  QUANT_FLOW_SKIP_EXISTING=1 QUANT_FLOW_COMBINE_ALL_EXISTING=1 \
  QUANT_FLOW_FETCH_NORTHBOUND_AGGREGATE=0 \
  QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS=1 \
  QUANT_FLOW_FETCH_MAIN_FUND=0 QUANT_FLOW_FETCH_BIG_DEAL=0 \
  QUANT_FLOW_FETCH_FUND_FLOW_RANKS=0 \
  QUANT_FLOW_FETCH_MARGIN_DETAILS=0 QUANT_FLOW_ALLOW_PARTIAL=1 \
  .venv/bin/python scripts/fetch_flow_signals.py
```

Result: failures=0.

| Code | Rows | Date start | Date end |
| --- | ---: | --- | --- |
| `000009` | 1,698 | 2017-03-16 | 2024-08-16 |
| `000019` | 1,594 | 2017-07-20 | 2024-08-16 |
| `000021` | 1,686 | 2017-03-16 | 2024-08-16 |
| `000027` | 1,683 | 2017-03-16 | 2024-08-16 |
| `000028` | 1,685 | 2017-03-16 | 2024-08-16 |

The combined Northbound holdings file now contains 13,001 rows across 8
symbols, including these 5 target-universe symbols. The next target offset is
`5`.

## Next Commands

Next Northbound batch:

```bash
env QUANT_FLOW_SKIP_EXISTING=1 QUANT_FLOW_COMBINE_ALL_EXISTING=1 \
  QUANT_FLOW_FETCH_NORTHBOUND_AGGREGATE=0 \
  QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS=1 \
  QUANT_FLOW_FETCH_MAIN_FUND=0 QUANT_FLOW_FETCH_BIG_DEAL=0 \
  QUANT_FLOW_FETCH_FUND_FLOW_RANKS=0 \
  QUANT_FLOW_FETCH_MARGIN_DETAILS=0 \
  QUANT_FLOW_SYMBOL_OFFSET=5 QUANT_FLOW_MAX_SYMBOLS=25 \
  .venv/bin/python scripts/fetch_flow_signals.py
```

First margin/short history batch:

```bash
env QUANT_FLOW_FETCH_NORTHBOUND_AGGREGATE=0 \
  QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS=0 \
  QUANT_FLOW_FETCH_MAIN_FUND=0 QUANT_FLOW_FETCH_BIG_DEAL=0 \
  QUANT_FLOW_FETCH_FUND_FLOW_RANKS=0 \
  QUANT_FLOW_FETCH_MARGIN_DETAILS=1 \
  QUANT_FLOW_MARGIN_START=20240101 QUANT_FLOW_MARGIN_END=20240126 \
  QUANT_FLOW_SKIP_EXISTING=1 \
  .venv/bin/python scripts/fetch_flow_signals.py
```

First intraday batch:

```bash
env QUANT_INTRADAY_SKIP_EXISTING=1 \
  QUANT_INTRADAY_COMBINE_ALL_EXISTING=1 \
  QUANT_INTRADAY_SYMBOL_OFFSET=0 QUANT_INTRADAY_MAX_SYMBOLS=25 \
  .venv/bin/python scripts/fetch_intraday_microstructure.py
```

## Production Blockers

- The Northbound probe is local public-provider research evidence only.
- Full production still needs provider entitlement evidence and external WORM
  attestation.
- Snapshot sources still need daily archival accumulation.
- Borrow availability remains missing and blocks real short/market-neutral
  sleeves.
