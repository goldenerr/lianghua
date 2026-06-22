#!/usr/bin/env python3
"""Audit whether V29 can make a credible 20-year full-universe claim."""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq
from _paths import DATA_DIR, RESULTS_DIR
from research_v28_factor_direction import DEFAULT_UNIVERSE, _sha256_file

REQUIRED_COLUMNS = ("close", "volume", "amount")
DEFAULT_OUTPUT_JSON = RESULTS_DIR / "quant_v29_20y_data_coverage_audit.json"
DEFAULT_SYMBOL_CSV = RESULTS_DIR / "quant_v29_20y_symbol_coverage.csv"
DEFAULT_DAILY_CSV = RESULTS_DIR / "quant_v29_20y_daily_cross_section.csv"


@dataclass(frozen=True)
class AuditConfig:
    universe: Path
    data_dir: Path
    start: pd.Timestamp
    end: pd.Timestamp
    warmup_days: int
    min_rows_per_year: int
    production_min_coverage_ratio: float
    diagnostic_min_symbols: int


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    parser.add_argument("--start-date", default="20060101")
    parser.add_argument("--end-date", default="20260529")
    parser.add_argument("--warmup-days", type=int, default=252)
    parser.add_argument("--min-rows-per-year", type=int, default=220)
    parser.add_argument("--production-min-coverage-ratio", type=float, default=0.95)
    parser.add_argument("--diagnostic-min-symbols", type=int, default=500)
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--symbol-csv", default=str(DEFAULT_SYMBOL_CSV))
    parser.add_argument("--daily-csv", default=str(DEFAULT_DAILY_CSV))
    return parser.parse_args()


def _parse_date(value: str) -> pd.Timestamp:
    return pd.Timestamp(datetime.strptime(value, "%Y%m%d")).normalize()


def read_codes(path: Path) -> list[str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("codes") or raw.get("symbols") or raw.get("data")
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise RuntimeError(f"universe must be a JSON string array or dict with codes: {path}")
    return [str(item).zfill(6) for item in raw]


def _available_parquet_columns(path: Path) -> list[str]:
    names = set(pq.ParquetFile(path).schema_arrow.names)
    columns = [column for column in ("date", *REQUIRED_COLUMNS) if column in names]
    if columns:
        return columns
    return []


def _load_price_frame(path: Path) -> tuple[pd.DataFrame, int, list[str]]:
    columns = _available_parquet_columns(path)
    frame = pd.read_parquet(path, columns=columns or None)
    missing_columns = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    raw_dates = frame["date"] if "date" in frame.columns else frame.index
    parsed_dates = pd.to_datetime(raw_dates, errors="coerce")
    valid_dates = pd.notna(parsed_dates)
    duplicate_dates = int(pd.DatetimeIndex(parsed_dates[valid_dates]).normalize().duplicated().sum())
    frame = frame.loc[valid_dates].copy()
    frame.index = pd.DatetimeIndex(parsed_dates[valid_dates]).normalize()
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    return frame, duplicate_dates, missing_columns


def _empty_symbol_row(code: str, path: Path, reason: str) -> dict[str, Any]:
    return {
        "code": code,
        "path": str(path),
        "exists": path.exists(),
        "load_error": reason,
        "first_date": None,
        "last_date": None,
        "calendar_years": 0.0,
        "rows_total": 0,
        "rows_in_window": 0,
        "duplicate_dates": 0,
        "missing_columns": ",".join(REQUIRED_COLUMNS),
        "close_nan_ratio": 1.0,
        "volume_nan_ratio": 1.0,
        "amount_nan_ratio": 1.0,
        "invalid_close_count": 0,
        "spans_requested_window": False,
        "strict_20y_quality_ok": False,
    }


def _symbol_row(
    code: str,
    path: Path,
    frame: pd.DataFrame,
    duplicate_dates: int,
    missing_columns: list[str],
    config: AuditConfig,
) -> dict[str, Any]:
    in_window = frame.loc[(frame.index >= config.start) & (frame.index <= config.end)]
    first_date = frame.index.min() if not frame.empty else None
    last_date = frame.index.max() if not frame.empty else None
    calendar_years = (
        round(float((last_date - first_date).days / 365.25), 2)
        if first_date is not None and last_date is not None
        else 0.0
    )
    requested_years = max(float((config.end - config.start).days / 365.25), 0.0)
    min_requested_rows = int(math.floor(requested_years * config.min_rows_per_year))
    spans_window = bool(first_date is not None and last_date is not None and first_date <= config.start and last_date >= config.end)
    ratios: dict[str, float] = {}
    for column in REQUIRED_COLUMNS:
        if column in in_window.columns and len(in_window) > 0:
            ratios[column] = round(float(pd.to_numeric(in_window[column], errors="coerce").isna().mean()), 6)
        else:
            ratios[column] = 1.0
    invalid_close_count = (
        int((pd.to_numeric(in_window["close"], errors="coerce") <= 0).sum()) if "close" in in_window.columns else 0
    )
    strict_quality_ok = bool(
        spans_window
        and not missing_columns
        and len(in_window) >= min_requested_rows
        and ratios["close"] <= 0.01
        and ratios["volume"] <= 0.01
        and ratios["amount"] <= 0.01
        and invalid_close_count == 0
    )
    return {
        "code": code,
        "path": str(path),
        "exists": True,
        "load_error": None,
        "first_date": first_date.date().isoformat() if first_date is not None else None,
        "last_date": last_date.date().isoformat() if last_date is not None else None,
        "calendar_years": calendar_years,
        "rows_total": int(len(frame)),
        "rows_in_window": int(len(in_window)),
        "duplicate_dates": duplicate_dates,
        "missing_columns": ",".join(missing_columns),
        "close_nan_ratio": ratios["close"],
        "volume_nan_ratio": ratios["volume"],
        "amount_nan_ratio": ratios["amount"],
        "invalid_close_count": invalid_close_count,
        "spans_requested_window": spans_window,
        "strict_20y_quality_ok": strict_quality_ok,
    }


def _series_stats(series: pd.Series) -> dict[str, Any]:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty:
        return {
            "min": 0,
            "p10": 0,
            "median": 0,
            "mean": 0.0,
            "p90": 0,
            "max": 0,
            "first_date": None,
            "last_date": None,
        }
    return {
        "min": int(clean.min()),
        "p10": int(clean.quantile(0.10)),
        "median": int(clean.median()),
        "mean": round(float(clean.mean()), 2),
        "p90": int(clean.quantile(0.90)),
        "max": int(clean.max()),
        "first_date": clean.index.min().date().isoformat(),
        "last_date": clean.index.max().date().isoformat(),
    }


def _first_date_at_threshold(series: pd.Series, threshold: int) -> str | None:
    hits = series[series >= threshold]
    if hits.empty:
        return None
    return hits.index.min().date().isoformat()


def _daily_cross_section(close: pd.DataFrame, config: AuditConfig) -> pd.DataFrame:
    if close.empty:
        return pd.DataFrame(columns=["date", "close_count", "warmup_eligible_count", "close_coverage_ratio"])
    in_window = close.loc[(close.index >= config.start) & (close.index <= config.end)].copy()
    close_count = in_window.notna().sum(axis=1)
    warmup_counts = close.notna().rolling(config.warmup_days, min_periods=config.warmup_days).sum()
    warmup_eligible = (warmup_counts >= config.warmup_days).sum(axis=1).reindex(in_window.index).fillna(0).astype(int)
    daily = pd.DataFrame(
        {
            "date": in_window.index.date.astype(str),
            "close_count": close_count.astype(int).to_numpy(),
            "warmup_eligible_count": warmup_eligible.to_numpy(),
            "close_coverage_ratio": (close_count / max(int(close.shape[1]), 1)).round(6).to_numpy(),
        },
        index=in_window.index,
    )
    return daily


def audit_coverage(codes: list[str], config: AuditConfig) -> tuple[dict[str, Any], list[dict[str, Any]], pd.DataFrame]:
    symbol_rows: list[dict[str, Any]] = []
    close_series: dict[str, pd.Series] = {}
    for code in codes:
        path = config.data_dir / f"{code}.parquet"
        if not path.exists():
            symbol_rows.append(_empty_symbol_row(code, path, "missing parquet"))
            continue
        try:
            frame, duplicate_dates, missing_columns = _load_price_frame(path)
        except Exception as exc:
            symbol_rows.append(_empty_symbol_row(code, path, f"{type(exc).__name__}: {exc}"))
            continue
        symbol_rows.append(_symbol_row(code, path, frame, duplicate_dates, missing_columns, config))
        if "close" in frame.columns:
            close_series[code] = pd.to_numeric(frame["close"], errors="coerce")

    close = pd.DataFrame(close_series).sort_index() if close_series else pd.DataFrame()
    daily = _daily_cross_section(close, config)
    total = len(codes)
    spans = sum(1 for row in symbol_rows if row["spans_requested_window"])
    strict = sum(1 for row in symbol_rows if row["strict_20y_quality_ok"])
    existing = sum(1 for row in symbol_rows if row["exists"] and not row["load_error"])
    min_production_symbols = int(math.ceil(total * config.production_min_coverage_ratio))
    threshold_dates = {
        str(threshold): _first_date_at_threshold(daily.set_index(pd.to_datetime(daily["date"]))["warmup_eligible_count"], threshold)
        for threshold in (100, 300, 500, 1000, 1500, 2000)
        if not daily.empty
    }
    cohort_cutoffs = ["20060101", "20080101", "20100101", "20150101", "20170101"]
    cohorts = {}
    for cutoff in cohort_cutoffs:
        ts = _parse_date(cutoff)
        cohorts[cutoff] = sum(
            1
            for row in symbol_rows
            if row["first_date"] is not None
            and pd.Timestamp(row["first_date"]) <= ts
            and row["last_date"] is not None
            and pd.Timestamp(row["last_date"]) >= config.end
        )

    warmup_series = (
        daily.set_index(pd.to_datetime(daily["date"]))["warmup_eligible_count"] if not daily.empty else pd.Series(dtype=float)
    )
    diagnostic_start = _first_date_at_threshold(warmup_series, config.diagnostic_min_symbols)
    production_start = _first_date_at_threshold(warmup_series, min_production_symbols)
    can_claim_full_20y = strict >= min_production_symbols and production_start is not None
    can_run_long_diagnostic = diagnostic_start is not None
    assessment = {
        "can_claim_20y_full_universe_production_validation": can_claim_full_20y,
        "can_run_20y_diagnostic_backtest": can_run_long_diagnostic,
        "diagnostic_start_after_warmup": diagnostic_start,
        "production_full_universe_start_after_warmup": production_start,
        "reason": (
            "20-year full-universe production validation coverage is sufficient."
            if can_claim_full_20y
            else "20-year run should be treated as a diagnostic unless PIT universe history and warmup cross-section coverage are expanded."
        ),
        "minimum_production_symbols": min_production_symbols,
        "diagnostic_min_symbols": config.diagnostic_min_symbols,
    }
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V29-20y-data-coverage-audit",
        "research_only": True,
        "requested_window": {
            "start_date": config.start.date().isoformat(),
            "end_date": config.end.date().isoformat(),
            "warmup_days": config.warmup_days,
            "min_rows_per_year": config.min_rows_per_year,
        },
        "universe": str(config.universe),
        "universe_sha256": _sha256_file(config.universe) if config.universe.exists() else None,
        "data_dir": str(config.data_dir),
        "summary": {
            "universe_symbols": total,
            "parquet_loaded_symbols": existing,
            "spans_requested_window_symbols": spans,
            "strict_20y_quality_symbols": strict,
            "strict_20y_quality_ratio": round(strict / total, 6) if total else 0.0,
            "symbols_with_close_series": int(close.shape[1]),
            "calendar_dates_in_window": int(len(daily)),
            "cohorts_with_first_date_before_cutoff_and_last_after_end": cohorts,
            "close_count_stats": _series_stats(daily.set_index(pd.to_datetime(daily["date"]))["close_count"]) if not daily.empty else {},
            "warmup_eligible_count_stats": _series_stats(warmup_series),
            "first_warmup_eligible_date_by_threshold": threshold_dates,
        },
        "assessment": assessment,
    }
    return payload, symbol_rows, daily


def _write_symbol_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "code",
        "exists",
        "load_error",
        "first_date",
        "last_date",
        "calendar_years",
        "rows_total",
        "rows_in_window",
        "duplicate_dates",
        "missing_columns",
        "close_nan_ratio",
        "volume_nan_ratio",
        "amount_nan_ratio",
        "invalid_close_count",
        "spans_requested_window",
        "strict_20y_quality_ok",
        "path",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})


def write_outputs(
    payload: dict[str, Any],
    symbol_rows: list[dict[str, Any]],
    daily: pd.DataFrame,
    *,
    output_json: Path,
    symbol_csv: Path,
    daily_csv: Path,
) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_symbol_csv(symbol_csv, symbol_rows)
    daily_csv.parent.mkdir(parents=True, exist_ok=True)
    daily.to_csv(daily_csv, index=False)


def main() -> None:
    args = _parse_args()
    config = AuditConfig(
        universe=Path(args.universe),
        data_dir=Path(args.data_dir),
        start=_parse_date(args.start_date),
        end=_parse_date(args.end_date),
        warmup_days=args.warmup_days,
        min_rows_per_year=args.min_rows_per_year,
        production_min_coverage_ratio=args.production_min_coverage_ratio,
        diagnostic_min_symbols=args.diagnostic_min_symbols,
    )
    codes = read_codes(config.universe)
    payload, symbol_rows, daily = audit_coverage(codes, config)
    write_outputs(
        payload,
        symbol_rows,
        daily,
        output_json=Path(args.output_json),
        symbol_csv=Path(args.symbol_csv),
        daily_csv=Path(args.daily_csv),
    )
    summary = payload["summary"]
    assessment = payload["assessment"]
    print(f"Wrote {args.output_json}")
    print(f"Wrote {args.symbol_csv}")
    print(f"Wrote {args.daily_csv}")
    print(
        "20y audit: "
        f"strict={summary['strict_20y_quality_symbols']}/{summary['universe_symbols']} "
        f"spans={summary['spans_requested_window_symbols']} "
        f"diagnostic_start={assessment['diagnostic_start_after_warmup']} "
        f"production_start={assessment['production_full_universe_start_after_warmup']} "
        f"claim_full_20y={assessment['can_claim_20y_full_universe_production_validation']}"
    )


if __name__ == "__main__":
    main()
