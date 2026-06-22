#!/usr/bin/env python3
"""Build V31 free-source PIT and trading-status approximations.

This script intentionally creates research-grade approximations from local
free/public data. It does not replace vendor PIT security master, exchange
trading-status history, corporate-action reconciliation, or broker fill logs.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from _paths import DATA_DIR, PROJECT_DIR, RESULTS_DIR

DEFAULT_UNIVERSE = PROJECT_DIR / "data" / "stock_list_provider_qualified_non_largecap_2000_v27.json"
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "data" / "security_master" / "free_pit_approx"
DEFAULT_CORPORATE_ACTION_OUTPUT = (
    PROJECT_DIR / "data" / "corporate_actions" / "free_reconciliation_v31.parquet"
)
DEFAULT_REPORT_JSON = RESULTS_DIR / "quant_v31_free_data_build_report.json"
DEFAULT_REPORT_CSV = RESULTS_DIR / "quant_v31_free_data_build_report.csv"
DEFAULT_START_DATE = os.getenv("QUANT_V31_FREE_START", "20060101")
DEFAULT_END_DATE = os.getenv("QUANT_V31_FREE_END", "20260529")
ADJUSTMENT_COLUMNS = {
    "adj_factor",
    "adjust_factor",
    "qfq_factor",
    "hfq_factor",
    "adjusted_close",
    "close_qfq",
    "close_hfq",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    parser.add_argument("--price-dir", default=str(DATA_DIR))
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--end-date", default=DEFAULT_END_DATE)
    parser.add_argument(
        "--max-symbols",
        type=int,
        default=int(os.getenv("QUANT_V31_FREE_MAX_SYMBOLS", "0")),
        help="0 means all symbols from universe",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(os.getenv("QUANT_V31_FREE_BATCH_SIZE", "100")),
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--corporate-action-output", default=str(DEFAULT_CORPORATE_ACTION_OUTPUT))
    parser.add_argument("--report-json", default=str(DEFAULT_REPORT_JSON))
    parser.add_argument("--report-csv", default=str(DEFAULT_REPORT_CSV))
    return parser.parse_args()


def _normalize_code(value: Any) -> str | None:
    match = re.search(r"(\d{6})", str(value))
    return match.group(1) if match else None


def _read_codes(path: Path) -> list[str]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    raw: Iterable[Any]
    if isinstance(loaded, list):
        raw = loaded
    elif isinstance(loaded, dict):
        raw = (
            loaded.get("codes")
            or loaded.get("symbols")
            or loaded.get("stock_codes")
            or loaded.get("universe")
            or []
        )
    else:
        raw = []
    codes: list[str] = []
    seen: set[str] = set()
    for item in raw:
        code = _normalize_code(item)
        if code and code not in seen:
            codes.append(code)
            seen.add(code)
    return codes


def _parse_yyyymmdd(value: str) -> pd.Timestamp:
    return pd.to_datetime(value, format="%Y%m%d", errors="raise").normalize()


def _standardize_price_frame(path: Path, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    frame = pd.read_parquet(path)
    if isinstance(frame.index, pd.DatetimeIndex):
        dates = pd.Series(frame.index, index=frame.index)
    elif "date" in frame.columns:
        dates = pd.to_datetime(frame["date"], errors="coerce")
    elif "日期" in frame.columns:
        dates = pd.to_datetime(frame["日期"], errors="coerce")
    else:
        raise ValueError(f"{path} has no date index or date column")
    frame = frame.copy()
    parsed_dates = pd.to_datetime(dates, errors="coerce")
    if isinstance(parsed_dates, pd.Series):
        frame.index = pd.DatetimeIndex(parsed_dates.dt.normalize())
    else:
        frame.index = pd.DatetimeIndex(parsed_dates).normalize()
    frame = frame.loc[frame.index.notna()]
    frame = frame.loc[(frame.index >= start) & (frame.index <= end)].sort_index()
    frame = frame[~frame.index.duplicated(keep="first")]
    return frame


def _load_csi300_codes() -> set[str]:
    path = PROJECT_DIR / "data" / "csi300_components_v26.json"
    if not path.exists():
        return set()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    raw = loaded.get("codes", []) if isinstance(loaded, dict) else loaded
    return {code for item in raw if (code := _normalize_code(item))}


def _price_limit_pct(code: str, dates: pd.DatetimeIndex) -> pd.Series:
    values: list[float] = []
    for dt in dates:
        if code.startswith(("688", "689")) or (
            code.startswith(("300", "301")) and dt >= pd.Timestamp("2020-08-24")
        ):
            values.append(0.20)
        elif code.startswith(("8", "4", "9")):
            values.append(0.30)
        else:
            values.append(0.10)
    return pd.Series(values, index=dates, dtype="float64")


def _first_pass(
    *,
    codes: list[str],
    price_dir: Path,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[dict[str, dict[str, Any]], pd.DatetimeIndex, list[dict[str, Any]]]:
    metadata: dict[str, dict[str, Any]] = {}
    all_dates: set[pd.Timestamp] = set()
    failures: list[dict[str, Any]] = []
    for code in codes:
        path = price_dir / f"{code}.parquet"
        if not path.exists():
            failures.append({"code": code, "failure": "missing_price_file", "path": str(path)})
            continue
        try:
            frame = _standardize_price_frame(path, start, end)
        except Exception as exc:
            failures.append(
                {"code": code, "failure": f"read_error:{type(exc).__name__}:{exc}", "path": str(path)}
            )
            continue
        if frame.empty:
            failures.append({"code": code, "failure": "empty_in_requested_window", "path": str(path)})
            continue
        all_dates.update(pd.Timestamp(item).normalize() for item in frame.index)
        present_adjustment_columns = sorted(ADJUSTMENT_COLUMNS.intersection(frame.columns))
        metadata[code] = {
            "path": str(path),
            "first_date": frame.index.min().normalize(),
            "last_date": frame.index.max().normalize(),
            "rows": int(len(frame)),
            "columns": list(frame.columns),
            "present_adjustment_columns": present_adjustment_columns,
        }
    return metadata, pd.DatetimeIndex(sorted(all_dates)), failures


def _make_symbol_tables(
    *,
    code: str,
    frame: pd.DataFrame,
    meta: dict[str, Any],
    all_dates: pd.DatetimeIndex,
    end: pd.Timestamp,
    csi300_codes: set[str],
    generated_at: datetime,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    first_date = pd.Timestamp(meta["first_date"]).normalize()
    last_date = pd.Timestamp(meta["last_date"]).normalize()
    listed_dates = all_dates[(all_dates >= first_date) & (all_dates <= last_date)]
    aligned = frame.reindex(listed_dates)

    close = pd.to_numeric(aligned.get("close"), errors="coerce")
    volume = pd.to_numeric(
        aligned.get("volume", pd.Series(index=listed_dates, dtype="float64")),
        errors="coerce",
    ).fillna(0.0)
    has_bar = close.notna()
    is_tradable = has_bar & (volume > 0)
    is_suspended = ~is_tradable
    prior_close = close.ffill().shift(1)
    pct_change = close / prior_close - 1.0
    limit_pct = _price_limit_pct(code, listed_dates)
    is_limit_up = (pct_change >= (limit_pct - 0.002)) & has_bar
    is_limit_down = (pct_change <= (-limit_pct + 0.002)) & has_bar
    inferred_delist_date = last_date if last_date < end else pd.NaT
    asof = generated_at.isoformat()
    source = "local_daily_parquet_free_pit_approx_v31"
    limitations = (
        "research_only;listing_dates_inferred_from_local_bars;"
        "st_status_unknown;price_limit_rule_approximation"
    )

    base = {
        "date": listed_dates,
        "code": code,
        "source": source,
        "asof_date": asof,
        "research_pit_approximation": True,
        "production_reconciled": False,
    }
    pit = pd.DataFrame(
        {
            **base,
            "name": "",
            "list_date": first_date,
            "delist_date": inferred_delist_date,
            "is_listed": True,
            "is_tradable": is_tradable.astype(bool).to_numpy(),
            "is_large_cap": code in csi300_codes,
            "large_cap_source": "akshare.index_stock_cons.000300_current_snapshot"
            if csi300_codes
            else "missing_current_csi300_snapshot",
            "delist_inferred_from_last_local_bar": bool(pd.notna(inferred_delist_date)),
            "limitations": limitations,
        }
    )
    pit["list_date"] = pd.to_datetime(pit["list_date"])
    pit["delist_date"] = pd.to_datetime(pit["delist_date"])

    status = pd.DataFrame(
        {
            **base,
            "is_st": False,
            "is_st_known": False,
            "is_suspended": is_suspended.astype(bool).to_numpy(),
            "is_limit_up": is_limit_up.fillna(False).astype(bool).to_numpy(),
            "is_limit_down": is_limit_down.fillna(False).astype(bool).to_numpy(),
            "is_tradable": is_tradable.astype(bool).to_numpy(),
            "price_limit_pct": limit_pct.to_numpy(),
            "status_quality": "free_approx",
            "limitations": limitations,
        }
    )

    present_adjustment_columns = meta.get("present_adjustment_columns", [])
    corp = pd.DataFrame(
        [
            {
                "date": generated_at.date().isoformat(),
                "code": code,
                "action_type": "free_adjustment_column_audit",
                "vendor_adjust_factor": None,
                "local_adjust_factor": None,
                "adjustment_diff_bps": None,
                "source": "local_daily_parquet_column_audit_v31",
                "evidence_ref": "",
                "has_adjustment_columns": bool(present_adjustment_columns),
                "adjustment_columns_present": ",".join(present_adjustment_columns),
                "production_reconciled": False,
                "limitations": (
                    "free audit only; missing vendor corporate-action reconciliation"
                    if not present_adjustment_columns
                    else "local adjustment columns detected but vendor reconciliation still missing"
                ),
                "asof_date": asof,
            }
        ]
    )
    return pit, status, corp


def _write_parquet_chunks(path: Path, chunks: Iterable[pd.DataFrame]) -> tuple[int, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    writer: pq.ParquetWriter | None = None
    rows = 0
    chunks_written = 0
    try:
        for chunk in chunks:
            if chunk.empty:
                continue
            table = pa.Table.from_pandas(chunk, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(path, table.schema, compression="zstd")
            writer.write_table(table)
            rows += int(len(chunk))
            chunks_written += 1
    finally:
        if writer is not None:
            writer.close()
    return rows, chunks_written


def _batched(values: list[str], size: int) -> Iterable[list[str]]:
    size = max(1, size)
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _build_outputs(
    *,
    codes: list[str],
    metadata: dict[str, dict[str, Any]],
    all_dates: pd.DatetimeIndex,
    price_dir: Path,
    output_dir: Path,
    corporate_action_output: Path,
    start: pd.Timestamp,
    end: pd.Timestamp,
    batch_size: int,
    generated_at: datetime,
) -> dict[str, Any]:
    loaded_codes = [code for code in codes if code in metadata]
    csi300_codes = _load_csi300_codes()
    pit_path = output_dir / "pit_universe_v31.parquet"
    status_path = output_dir / "trading_status_v31.parquet"

    pit_chunks: list[pd.DataFrame] = []
    status_chunks: list[pd.DataFrame] = []
    corp_chunks: list[pd.DataFrame] = []
    pit_rows = 0
    status_rows = 0
    pit_writer: pq.ParquetWriter | None = None
    status_writer: pq.ParquetWriter | None = None

    for path in (pit_path, status_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()

    try:
        for batch in _batched(loaded_codes, batch_size):
            pit_chunks.clear()
            status_chunks.clear()
            corp_chunks.clear()
            for code in batch:
                frame = _standardize_price_frame(price_dir / f"{code}.parquet", start, end)
                pit, status, corp = _make_symbol_tables(
                    code=code,
                    frame=frame,
                    meta=metadata[code],
                    all_dates=all_dates,
                    end=end,
                    csi300_codes=csi300_codes,
                    generated_at=generated_at,
                )
                pit_chunks.append(pit)
                status_chunks.append(status)
                corp_chunks.append(corp)
            pit_batch = pd.concat(pit_chunks, ignore_index=True)
            status_batch = pd.concat(status_chunks, ignore_index=True)
            pit_table = pa.Table.from_pandas(pit_batch, preserve_index=False)
            status_table = pa.Table.from_pandas(status_batch, preserve_index=False)
            if pit_writer is None:
                pit_writer = pq.ParquetWriter(pit_path, pit_table.schema, compression="zstd")
            if status_writer is None:
                status_writer = pq.ParquetWriter(status_path, status_table.schema, compression="zstd")
            pit_writer.write_table(pit_table)
            status_writer.write_table(status_table)
            pit_rows += int(len(pit_batch))
            status_rows += int(len(status_batch))
            yield {
                "batch_codes": len(batch),
                "pit_rows": pit_rows,
                "status_rows": status_rows,
                "corp_chunks": corp_chunks,
            }
    finally:
        if pit_writer is not None:
            pit_writer.close()
        if status_writer is not None:
            status_writer.close()

    corp_frames: list[pd.DataFrame] = []
    for batch in _batched(loaded_codes, batch_size):
        rows: list[pd.DataFrame] = []
        for code in batch:
            dummy_frame = _standardize_price_frame(price_dir / f"{code}.parquet", start, end)
            _, _, corp = _make_symbol_tables(
                code=code,
                frame=dummy_frame,
                meta=metadata[code],
                all_dates=all_dates,
                end=end,
                csi300_codes=csi300_codes,
                generated_at=generated_at,
            )
            rows.append(corp)
        if rows:
            corp_frames.append(pd.concat(rows, ignore_index=True))
    corp_all = pd.concat(corp_frames, ignore_index=True) if corp_frames else pd.DataFrame()
    corporate_action_output.parent.mkdir(parents=True, exist_ok=True)
    corp_all.to_parquet(corporate_action_output, index=False)

    return {
        "pit_path": str(pit_path),
        "trading_status_path": str(status_path),
        "corporate_action_path": str(corporate_action_output),
        "pit_rows": pit_rows,
        "trading_status_rows": status_rows,
        "corporate_action_rows": int(len(corp_all)),
        "csi300_snapshot_symbols": len(csi300_codes),
    }


def _write_report_csv(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "version",
        "symbols_requested",
        "symbols_loaded",
        "unique_trading_dates",
        "pit_rows",
        "trading_status_rows",
        "corporate_action_rows",
        "research_pit_approximation",
        "production_data_ready",
        "failure_count",
    ]
    row = {
        "version": report["version"],
        "symbols_requested": report["summary"]["symbols_requested"],
        "symbols_loaded": report["summary"]["symbols_loaded"],
        "unique_trading_dates": report["summary"]["unique_trading_dates"],
        "pit_rows": report["outputs"]["pit_rows"],
        "trading_status_rows": report["outputs"]["trading_status_rows"],
        "corporate_action_rows": report["outputs"]["corporate_action_rows"],
        "research_pit_approximation": report["research_pit_approximation"],
        "production_data_ready": report["production_data_ready"],
        "failure_count": len(report["failures"]),
    }
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow(row)


def main() -> None:
    args = _parse_args()
    generated_at = datetime.now(timezone.utc)
    start = _parse_yyyymmdd(args.start_date)
    end = _parse_yyyymmdd(args.end_date)
    universe_path = Path(args.universe)
    price_dir = Path(args.price_dir)
    output_dir = Path(args.output_dir)
    corporate_action_output = Path(args.corporate_action_output)

    codes = _read_codes(universe_path)
    if args.max_symbols > 0:
        codes = codes[: args.max_symbols]
    metadata, all_dates, failures = _first_pass(
        codes=codes,
        price_dir=price_dir,
        start=start,
        end=end,
    )

    progress_iter = _build_outputs(
        codes=codes,
        metadata=metadata,
        all_dates=all_dates,
        price_dir=price_dir,
        output_dir=output_dir,
        corporate_action_output=corporate_action_output,
        start=start,
        end=end,
        batch_size=args.batch_size,
        generated_at=generated_at,
    )
    outputs: dict[str, Any] = {}
    for progress in progress_iter:
        outputs = {
            "pit_path": str(output_dir / "pit_universe_v31.parquet"),
            "trading_status_path": str(output_dir / "trading_status_v31.parquet"),
            "corporate_action_path": str(corporate_action_output),
            "pit_rows": progress["pit_rows"],
            "trading_status_rows": progress["status_rows"],
            "corporate_action_rows": int(sum(len(item) for item in progress["corp_chunks"])),
            "csi300_snapshot_symbols": len(_load_csi300_codes()),
        }
        print(
            "V31 free PIT build progress: "
            f"batch_codes={progress['batch_codes']} "
            f"pit_rows={progress['pit_rows']} "
            f"status_rows={progress['status_rows']}",
            flush=True,
        )
    if metadata:
        outputs = {
            "pit_path": str(output_dir / "pit_universe_v31.parquet"),
            "trading_status_path": str(output_dir / "trading_status_v31.parquet"),
            "corporate_action_path": str(corporate_action_output),
            "pit_rows": int(pq.ParquetFile(output_dir / "pit_universe_v31.parquet").metadata.num_rows),
            "trading_status_rows": int(
                pq.ParquetFile(output_dir / "trading_status_v31.parquet").metadata.num_rows
            ),
            "corporate_action_rows": int(pd.read_parquet(corporate_action_output, columns=["code"]).shape[0]),
            "csi300_snapshot_symbols": len(_load_csi300_codes()),
        }
    else:
        outputs = {
            "pit_path": str(output_dir / "pit_universe_v31.parquet"),
            "trading_status_path": str(output_dir / "trading_status_v31.parquet"),
            "corporate_action_path": str(corporate_action_output),
            "pit_rows": 0,
            "trading_status_rows": 0,
            "corporate_action_rows": 0,
            "csi300_snapshot_symbols": len(_load_csi300_codes()),
        }

    first_dates = [pd.Timestamp(item["first_date"]) for item in metadata.values()]
    last_dates = [pd.Timestamp(item["last_date"]) for item in metadata.values()]
    report = {
        "ts": generated_at.isoformat(),
        "version": "V31-free-data-pit-approximation",
        "research_only": True,
        "research_pit_approximation": True,
        "production_data_ready": False,
        "universe_path": str(universe_path),
        "price_dir": str(price_dir),
        "requested_window": {
            "start_date": start.date().isoformat(),
            "end_date": end.date().isoformat(),
        },
        "summary": {
            "symbols_requested": len(codes),
            "symbols_loaded": len(metadata),
            "unique_trading_dates": int(len(all_dates)),
            "date_start": all_dates.min().date().isoformat() if len(all_dates) else None,
            "date_end": all_dates.max().date().isoformat() if len(all_dates) else None,
            "earliest_observed_symbol_date": min(first_dates).date().isoformat()
            if first_dates
            else None,
            "latest_observed_symbol_date": max(last_dates).date().isoformat() if last_dates else None,
            "symbols_with_adjustment_columns": sum(
                bool(item.get("present_adjustment_columns")) for item in metadata.values()
            ),
            "symbols_inferred_delisted_or_incomplete": sum(
                pd.Timestamp(item["last_date"]).normalize() < end for item in metadata.values()
            ),
        },
        "outputs": outputs,
        "failures": failures[:500],
        "failure_count": len(failures),
        "limitations": [
            "上市/退市日期从本地日线可见区间推断，不是真正交易所或 vendor PIT security master。",
            "ST 状态在免费本地数据中不可可靠回放，当前输出显式标记 is_st_known=false。",
            "停牌由交易日缺 bar 或成交量为 0 近似推断，无法覆盖所有交易所状态细节。",
            "涨跌停按板块规则近似，不含所有历史规则、ST 5% 规则和特殊处理。",
            "公司行动只做本地调整列审计，不含 vendor corporate-action reconciliation。",
            "该输出不能解除 V30 生产数据 evidence blocker。",
        ],
        "next_required_work": [
            "如需生产解除 blocker，仍需 vendor PIT security master 和交易状态历史 evidence ref。",
            "如需真实执行验证，仍需 broker/exchange tick/minute/order/fill 历史。",
            "如坚持免费路线，应每日 WORM 归档公开源快照，逐日累积 future PIT 证据。",
        ],
    }
    report_json = Path(args.report_json)
    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report_csv(Path(args.report_csv), report)
    print(
        "V31 free PIT approximation: "
        f"symbols={len(metadata)}/{len(codes)} dates={len(all_dates)} "
        f"pit_rows={outputs['pit_rows']} production_data_ready=False",
        flush=True,
    )
    print(f"Wrote {report_json}", flush=True)
    print(f"Wrote {args.report_csv}", flush=True)


if __name__ == "__main__":
    main()
