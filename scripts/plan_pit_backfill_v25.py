#!/usr/bin/env python3
"""Plan incremental PIT alpha-panel backfills.

The planner is intentionally read-only. It emits batch commands for the
fetchers so large universe/data backfills can progress without repeatedly
refetching everything or overwriting combined feature files with one batch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from _paths import FLOW_SIGNALS_DIR, INTRADAY_DIR, PROJECT_DIR, RESULTS_DIR, STOCK_LIST

DEFAULT_OUTPUT_JSON = "quant_pit_backfill_plan_v25.json"
DEFAULT_OUTPUT_CSV = "quant_pit_backfill_plan_v25.csv"
DEFAULT_MARGIN_CALENDAR = PROJECT_DIR / "data" / "benchmarks" / "idx_000300.parquet"
DEFAULT_NORTHBOUND_UNAVAILABLE = (
    PROJECT_DIR / "data" / "flow_signals" / "northbound_unavailable_symbols_v26.json"
)


@dataclass(frozen=True)
class SymbolBatch:
    panel: str
    offset: int
    max_symbols: int
    missing_symbols: int
    first_code: str
    last_code: str
    command: str


@dataclass(frozen=True)
class DateBatch:
    panel: str
    start: str
    end: str
    missing_dates: int
    command: str


@dataclass(frozen=True)
class MarginCalendar:
    source: str
    dates: list[str]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--northbound-batch-size",
        type=int,
        default=int(os.getenv("QUANT_V25_NORTHBOUND_BATCH_SIZE", "100")),
        help="Contiguous target-universe symbol count per northbound batch.",
    )
    parser.add_argument(
        "--northbound-workers",
        type=int,
        default=int(os.getenv("QUANT_V25_NORTHBOUND_WORKERS", "4")),
        help="Worker count emitted for northbound fetch commands.",
    )
    parser.add_argument(
        "--intraday-batch-size",
        type=int,
        default=int(os.getenv("QUANT_V25_INTRADAY_BATCH_SIZE", "100")),
        help="Contiguous target-universe symbol count per intraday batch.",
    )
    parser.add_argument(
        "--intraday-workers",
        type=int,
        default=int(os.getenv("QUANT_V25_INTRADAY_WORKERS", "4")),
        help="Worker count emitted for intraday fetch commands.",
    )
    parser.add_argument(
        "--margin-start",
        default=os.getenv("QUANT_V25_MARGIN_START", "20240101"),
        help="First business date to check for margin/short backfill.",
    )
    parser.add_argument(
        "--margin-end",
        default=os.getenv("QUANT_V25_MARGIN_END", "20260529"),
        help="Last business date to check for margin/short backfill.",
    )
    parser.add_argument(
        "--margin-batch-days",
        type=int,
        default=int(os.getenv("QUANT_V25_MARGIN_BATCH_DAYS", "100")),
        help="Business-day count per margin/short date batch.",
    )
    parser.add_argument(
        "--margin-workers",
        type=int,
        default=int(os.getenv("QUANT_V25_MARGIN_WORKERS", "6")),
        help="Worker count emitted for margin/short fetch commands.",
    )
    parser.add_argument(
        "--margin-task-timeout-seconds",
        type=float,
        default=float(os.getenv("QUANT_V25_MARGIN_TASK_TIMEOUT_SECONDS", "45")),
        help="Per date/exchange task timeout emitted for margin/short fetch commands.",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=int(os.getenv("QUANT_V25_MAX_BATCHES", "0")),
        help="Optional cap per batch family. 0 means no cap.",
    )
    parser.add_argument(
        "--output-json",
        default=os.getenv("QUANT_V25_OUTPUT_JSON", DEFAULT_OUTPUT_JSON),
        help="Output JSON path or filename under data/backtest_results.",
    )
    parser.add_argument(
        "--output-csv",
        default=os.getenv("QUANT_V25_OUTPUT_CSV", DEFAULT_OUTPUT_CSV),
        help="Output CSV path or filename under data/backtest_results.",
    )
    return parser.parse_args()


def _resolve(path_value: str, default_dir: Path) -> Path:
    path = Path(path_value)
    if path.is_absolute() or path.parent != Path("."):
        return path
    return default_dir / path


def _load_codes() -> list[str]:
    raw = json.loads(STOCK_LIST.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise RuntimeError(f"stock list must be a JSON string array: {STOCK_LIST}")
    return [str(item).zfill(6) for item in raw]


def _stock_list_sha256(codes: list[str]) -> str:
    payload = json.dumps(codes, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _existing_symbol_stems(directory: Path) -> set[str]:
    if not directory.exists():
        return set()
    return {path.stem.zfill(6) for path in directory.glob("*.parquet")}


def _load_unavailable_symbols(path: Path) -> set[str]:
    """Load provider-unavailable symbols to keep plans moving without faking coverage."""

    if not path.exists():
        return set()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        values = raw
    elif isinstance(raw, dict):
        values = raw.get("symbols") or raw.get("codes") or []
    else:
        values = []
    return {str(item).zfill(6) for item in values if isinstance(item, str)}


def _shell_env(env: dict[str, str], script: str) -> str:
    pieces = ["env"]
    pieces.extend(f"{key}={value}" for key, value in env.items())
    pieces.extend([".venv/bin/python", script])
    return " ".join(pieces)


def _symbol_batches(
    *,
    panel: str,
    codes: list[str],
    existing: set[str],
    batch_size: int,
    command_env: dict[str, str],
    script: str,
    max_batches: int,
) -> list[SymbolBatch]:
    if batch_size <= 0:
        raise ValueError(f"{panel} batch size must be positive")
    missing_offsets = [idx for idx, code in enumerate(codes) if code not in existing]
    if not missing_offsets:
        return []
    batches: list[SymbolBatch] = []
    cursor = 0
    while cursor < len(missing_offsets):
        start_offset = missing_offsets[cursor]
        end_offset = min(start_offset + batch_size, len(codes))
        selected = codes[start_offset:end_offset]
        missing_in_batch = sum(code not in existing for code in selected)
        env = {
            **command_env,
            f"QUANT_{panel.upper()}_SYMBOL_OFFSET": str(start_offset),
            f"QUANT_{panel.upper()}_MAX_SYMBOLS": str(len(selected)),
        }
        if panel == "flow":
            env = {
                **command_env,
                "QUANT_FLOW_SYMBOL_OFFSET": str(start_offset),
                "QUANT_FLOW_MAX_SYMBOLS": str(len(selected)),
            }
        elif panel == "intraday":
            env = {
                **command_env,
                "QUANT_INTRADAY_SYMBOL_OFFSET": str(start_offset),
                "QUANT_INTRADAY_MAX_SYMBOLS": str(len(selected)),
            }
        batches.append(
            SymbolBatch(
                panel=panel,
                offset=start_offset,
                max_symbols=len(selected),
                missing_symbols=missing_in_batch,
                first_code=selected[0],
                last_code=selected[-1],
                command=_shell_env(env, script),
            )
        )
        if max_batches and len(batches) >= max_batches:
            break
        cursor += sum(1 for offset in missing_offsets[cursor:] if offset < end_offset)
    return batches


def _margin_existing_dates() -> set[str]:
    dates: set[str] = set()
    for path in FLOW_SIGNALS_DIR.glob("margin_sse_*.parquet"):
        date = path.stem.removeprefix("margin_sse_")
        if (FLOW_SIGNALS_DIR / f"margin_szse_{date}.parquet").exists():
            dates.add(date)
    return dates


def _load_margin_calendar(start: str, end: str) -> MarginCalendar:
    """Load A-share trading dates for margin backfill planning.

    A plain business-day range includes mainland exchange holidays such as
    New Year's Day. Use the local benchmark index calendar when available, then
    fall back to business days only when no benchmark calendar exists.
    """

    path = Path(os.getenv("QUANT_V25_MARGIN_CALENDAR_FILE", str(DEFAULT_MARGIN_CALENDAR)))
    if path.exists():
        frame = pd.read_parquet(path)
        if isinstance(frame.index, pd.DatetimeIndex):
            dates = pd.to_datetime(frame.index, errors="coerce")
        elif "date" in frame.columns:
            dates = pd.to_datetime(frame["date"], errors="coerce")
        else:
            dates = pd.DatetimeIndex([])
        expected = [
            date.strftime("%Y%m%d")
            for date in dates.dropna().normalize()
            if pd.Timestamp(start) <= date <= pd.Timestamp(end)
        ]
        if expected:
            return MarginCalendar(source=str(path), dates=sorted(set(expected)))
    expected = [date.strftime("%Y%m%d") for date in pd.bdate_range(start=start, end=end)]
    return MarginCalendar(source="pandas.bdate_range_fallback", dates=expected)


def _date_batches(
    *,
    start: str,
    end: str,
    batch_days: int,
    margin_workers: int,
    margin_task_timeout_seconds: float,
    max_batches: int,
) -> list[DateBatch]:
    if batch_days <= 0:
        raise ValueError("margin batch days must be positive")
    expected = _load_margin_calendar(start, end).dates
    existing = _margin_existing_dates()
    missing = [date for date in expected if date not in existing]
    batches: list[DateBatch] = []
    cursor = 0
    while cursor < len(missing):
        chunk = missing[cursor : cursor + batch_days]
        env = {
            "QUANT_FLOW_FETCH_NORTHBOUND_AGGREGATE": "0",
            "QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS": "0",
            "QUANT_FLOW_FETCH_MAIN_FUND": "0",
            "QUANT_FLOW_FETCH_BIG_DEAL": "0",
            "QUANT_FLOW_FETCH_FUND_FLOW_RANKS": "0",
            "QUANT_FLOW_FETCH_MARGIN_DETAILS": "1",
            "QUANT_FLOW_COMBINE_ALL_EXISTING": "1",
            "QUANT_FLOW_MARGIN_MAX_WORKERS": str(max(1, margin_workers)),
            "QUANT_FLOW_MARGIN_TASK_TIMEOUT_SECONDS": str(
                max(1.0, margin_task_timeout_seconds)
            ),
            "QUANT_FLOW_MARGIN_START": chunk[0],
            "QUANT_FLOW_MARGIN_END": chunk[-1],
            "QUANT_FLOW_SKIP_EXISTING": "1",
        }
        batches.append(
            DateBatch(
                panel="margin_short_daily_history",
                start=chunk[0],
                end=chunk[-1],
                missing_dates=len(chunk),
                command=_shell_env(env, "scripts/fetch_flow_signals.py"),
            )
        )
        if max_batches and len(batches) >= max_batches:
            break
        cursor += batch_days
    return batches


def _snapshot_commands() -> list[dict[str, str]]:
    flow_env = {
        "QUANT_FLOW_FETCH_NORTHBOUND_AGGREGATE": "1",
        "QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS": "0",
        "QUANT_FLOW_FETCH_MAIN_FUND": "1",
        "QUANT_FLOW_FETCH_BIG_DEAL": "1",
        "QUANT_FLOW_FETCH_FUND_FLOW_RANKS": "1",
        "QUANT_FLOW_FETCH_MARGIN_DETAILS": "0",
        "QUANT_FLOW_SKIP_EXISTING": "0",
    }
    archive_cmd = ".venv/bin/python scripts/run_alt_archive_daily_pipeline.py --archive-date $(date +%Y%m%d)"
    return [
        {
            "panel": "daily_flow_snapshot",
            "cadence": "daily_after_close",
            "command": _shell_env(flow_env, "scripts/fetch_flow_signals.py"),
        },
        {
            "panel": "daily_alt_archive_pipeline",
            "cadence": "daily_after_all_snapshot_fetches",
            "command": archive_cmd,
        },
    ]


def _asdict_batch(batch: SymbolBatch | DateBatch) -> dict[str, Any]:
    return batch.__dict__.copy()


def main() -> None:
    args = _parse_args()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output_json_path = _resolve(str(args.output_json), RESULTS_DIR)
    output_csv_path = _resolve(str(args.output_csv), RESULTS_DIR)
    codes = _load_codes()

    northbound_existing = _existing_symbol_stems(FLOW_SIGNALS_DIR / "northbound_holdings")
    intraday_existing = _existing_symbol_stems(INTRADAY_DIR / "features")
    northbound_unavailable_path = Path(
        os.getenv("QUANT_V25_NORTHBOUND_UNAVAILABLE_FILE", str(DEFAULT_NORTHBOUND_UNAVAILABLE))
    )
    northbound_unavailable = _load_unavailable_symbols(northbound_unavailable_path).intersection(
        set(codes)
    )

    northbound_batches = _symbol_batches(
        panel="flow",
        codes=codes,
        existing=northbound_existing.union(northbound_unavailable),
        batch_size=args.northbound_batch_size,
        script="scripts/fetch_flow_signals.py",
        command_env={
            "QUANT_STOCK_LIST": str(STOCK_LIST),
            "QUANT_FLOW_SKIP_EXISTING": "1",
            "QUANT_FLOW_COMBINE_ALL_EXISTING": "1",
            "QUANT_FLOW_MAX_WORKERS": str(max(1, args.northbound_workers)),
            "QUANT_FLOW_FETCH_NORTHBOUND_AGGREGATE": "0",
            "QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS": "1",
            "QUANT_FLOW_FETCH_MAIN_FUND": "0",
            "QUANT_FLOW_FETCH_BIG_DEAL": "0",
            "QUANT_FLOW_FETCH_FUND_FLOW_RANKS": "0",
            "QUANT_FLOW_FETCH_MARGIN_DETAILS": "0",
        },
        max_batches=args.max_batches,
    )
    intraday_batches = _symbol_batches(
        panel="intraday",
        codes=codes,
        existing=intraday_existing,
        batch_size=args.intraday_batch_size,
        script="scripts/fetch_intraday_microstructure.py",
        command_env={
            "QUANT_STOCK_LIST": str(STOCK_LIST),
            "QUANT_INTRADAY_SKIP_EXISTING": "1",
            "QUANT_INTRADAY_COMBINE_ALL_EXISTING": "1",
            "QUANT_INTRADAY_MAX_WORKERS": str(max(1, args.intraday_workers)),
        },
        max_batches=args.max_batches,
    )
    margin_batches = _date_batches(
        start=args.margin_start,
        end=args.margin_end,
        batch_days=args.margin_batch_days,
        margin_workers=args.margin_workers,
        margin_task_timeout_seconds=args.margin_task_timeout_seconds,
        max_batches=args.max_batches,
    )
    margin_calendar = _load_margin_calendar(args.margin_start, args.margin_end)

    all_rows = (
        [_asdict_batch(batch) for batch in northbound_batches]
        + [_asdict_batch(batch) for batch in intraday_batches]
        + [_asdict_batch(batch) for batch in margin_batches]
        + _snapshot_commands()
    )
    report = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V25-pit-backfill-plan",
        "research_only": True,
        "universe_size": len(codes),
        "target_universe": {
            "path": str(STOCK_LIST),
            "size": len(codes),
            "sha256": _stock_list_sha256(codes),
            "minimum_required_symbols": int(os.getenv("QUANT_V25_MIN_TARGET_SYMBOLS", "2000")),
            "passes_minimum_size": len(codes)
            >= int(os.getenv("QUANT_V25_MIN_TARGET_SYMBOLS", "2000")),
        },
        "coverage": {
            "northbound_existing_target_symbols": len(set(codes).intersection(northbound_existing)),
            "northbound_provider_unavailable_target_symbols": len(northbound_unavailable),
            "northbound_missing_target_symbols": len(
                [code for code in codes if code not in northbound_existing]
            ),
            "northbound_fetchable_missing_target_symbols": len(
                [
                    code
                    for code in codes
                    if code not in northbound_existing and code not in northbound_unavailable
                ]
            ),
            "intraday_existing_target_symbols": len(set(codes).intersection(intraday_existing)),
            "intraday_missing_target_symbols": len(
                [code for code in codes if code not in intraday_existing]
            ),
            "margin_existing_complete_dates": len(_margin_existing_dates()),
            "margin_expected_trading_dates": len(margin_calendar.dates),
        },
        "batch_counts": {
            "northbound": len(northbound_batches),
            "intraday": len(intraday_batches),
            "margin": len(margin_batches),
            "daily_snapshot": len(_snapshot_commands()),
        },
        "parameters": {
            "northbound_batch_size": args.northbound_batch_size,
            "northbound_workers": max(1, args.northbound_workers),
            "intraday_batch_size": args.intraday_batch_size,
            "intraday_workers": max(1, args.intraday_workers),
            "margin_start": args.margin_start,
            "margin_end": args.margin_end,
            "margin_batch_days": args.margin_batch_days,
            "margin_workers": max(1, args.margin_workers),
            "margin_task_timeout_seconds": max(1.0, args.margin_task_timeout_seconds),
            "margin_calendar_source": margin_calendar.source,
            "northbound_unavailable_file": str(northbound_unavailable_path),
            "max_batches": args.max_batches,
        },
        "provider_unavailable": {
            "northbound_stock_holding_symbols": sorted(northbound_unavailable),
            "note": (
                "These symbols are not counted as covered. They are excluded from batch "
                "commands only because repeated public-provider calls returned no usable panel."
            ),
        },
        "batches": {
            "northbound": [_asdict_batch(batch) for batch in northbound_batches],
            "intraday": [_asdict_batch(batch) for batch in intraday_batches],
            "margin": [_asdict_batch(batch) for batch in margin_batches],
            "daily_snapshots": _snapshot_commands(),
        },
        "production_blockers": [
            "plan emits local research commands only; provider entitlement/WORM evidence is still required",
            "snapshot commands must be run and archived daily before PIT history is sufficient",
            "borrow availability remains absent and needs a broker-backed provider",
        ],
    }
    output_json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    pd.DataFrame(all_rows).to_csv(output_csv_path, index=False)
    print(
        "V25 PIT backfill plan: "
        f"northbound_batches={len(northbound_batches)} "
        f"intraday_batches={len(intraday_batches)} "
        f"margin_batches={len(margin_batches)} "
        f"northbound_missing={report['coverage']['northbound_missing_target_symbols']} "
        f"intraday_missing={report['coverage']['intraday_missing_target_symbols']}",
        flush=True,
    )
    print(f"Wrote {output_json_path}", flush=True)
    print(f"Wrote {output_csv_path}", flush=True)


if __name__ == "__main__":
    main()
