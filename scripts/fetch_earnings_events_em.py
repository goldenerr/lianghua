#!/usr/bin/env python3
"""Fetch A-share earnings forecast/flash events from EastMoney via AkShare.

The output is research data only. Strategies must use ``announcement_date`` as
the point-in-time availability boundary; ``report_date`` alone is not sufficient
and would create look-ahead bias.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from _paths import EARNINGS_EVENTS_DIR

EARNINGS_EVENTS_DIR.mkdir(parents=True, exist_ok=True)

START_REPORT = os.getenv("QUANT_EARNINGS_EVENTS_START_REPORT", "20081231")
END_REPORT = os.getenv("QUANT_EARNINGS_EVENTS_END_REPORT", "20260529")
SLEEP_SECONDS = float(os.getenv("QUANT_EARNINGS_EVENTS_SLEEP_SECONDS", "0.2"))
RETRIES = int(os.getenv("QUANT_EARNINGS_EVENTS_RETRIES", "3"))
SKIP_EXISTING = os.getenv("QUANT_EARNINGS_EVENTS_SKIP_EXISTING", "1") == "1"
MIN_ROWS_TOTAL = int(os.getenv("QUANT_EARNINGS_EVENTS_MIN_ROWS_TOTAL", "100"))

UNIT_MULTIPLIERS = {
    "万亿": 1_000_000_000_000.0,
    "亿元": 100_000_000.0,
    "亿": 100_000_000.0,
    "万元": 10_000.0,
    "万": 10_000.0,
}


def _import_akshare() -> Any:
    try:
        import akshare as ak
    except ModuleNotFoundError as exc:
        raise RuntimeError("akshare is required to fetch EastMoney earnings events") from exc
    return ak


def _quarter_dates(start: str, end: str) -> list[str]:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    dates: list[str] = []
    for year in range(start_ts.year, end_ts.year + 1):
        for suffix in ("0331", "0630", "0930", "1231"):
            text = f"{year}{suffix}"
            ts = pd.Timestamp(text)
            if start_ts <= ts <= end_ts:
                dates.append(text)
    return dates


def _find_column(columns: list[str], *patterns: str) -> str | None:
    for pattern in patterns:
        for column in columns:
            if pattern in str(column):
                return str(column)
    return None


def _parse_number(value: Any, *, percent: bool = False) -> float | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "--", "None", "nan"}:
        return None
    if text.endswith("%"):
        percent = True
        text = text[:-1]
    multiplier = 1.0
    for unit, unit_multiplier in UNIT_MULTIPLIERS.items():
        if unit in text:
            multiplier = unit_multiplier
            text = text.replace(unit, "")
            break
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    value_float = float(match.group(0)) * multiplier
    return value_float / 100.0 if percent else value_float


def _parse_range(value: Any, *, percent: bool = False) -> tuple[float | None, float | None]:
    if value is None or pd.isna(value):
        return None, None
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "--", "None", "nan"}:
        return None, None
    percent = percent or "%" in text
    multiplier = 1.0
    for unit, unit_multiplier in UNIT_MULTIPLIERS.items():
        if unit in text:
            multiplier = unit_multiplier
            text = text.replace(unit, "")
            break
    numbers = [float(match) * multiplier for match in re.findall(r"[-+]?\d+(?:\.\d+)?", text)]
    if not numbers:
        return None, None
    if percent:
        numbers = [number / 100.0 for number in numbers]
    return min(numbers), max(numbers)


def _normalize_common(raw: pd.DataFrame, report_date: str, event_type: str) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()
    df = raw.copy()
    columns = [str(column) for column in df.columns]
    code_col = _find_column(columns, "股票代码", "代码")
    name_col = _find_column(columns, "股票简称", "简称", "名称")
    announcement_col = _find_column(columns, "公告日期", "最新公告日期", "披露日期")
    if code_col is None:
        raise RuntimeError(f"{event_type} {report_date}: missing stock code column")
    out = pd.DataFrame()
    out["code"] = df[code_col].astype(str).str.extract(r"(\d{6})", expand=False)
    out["name"] = df[name_col].astype(str) if name_col else pd.NA
    out["report_date"] = pd.Timestamp(report_date)
    if announcement_col:
        out["announcement_date"] = pd.to_datetime(df[announcement_col], errors="coerce")
    else:
        out["announcement_date"] = pd.NaT
    out["event_type"] = event_type
    out["source"] = "eastmoney_akshare"
    out["fetched_at"] = datetime.now().isoformat()
    return out


def _normalize_forecast(raw: pd.DataFrame, report_date: str) -> pd.DataFrame:
    out = _normalize_common(raw, report_date, "forecast")
    if out.empty:
        return out
    df = raw.copy()
    columns = [str(column) for column in df.columns]
    forecast_type_col = _find_column(columns, "预告类型", "业绩变动", "类型")
    reason_col = _find_column(columns, "业绩变动原因", "原因")
    yoy_col = _find_column(columns, "变动幅度", "同比")
    value_col = _find_column(columns, "预测数值", "净利润")
    out["forecast_type"] = df[forecast_type_col].astype(str) if forecast_type_col else pd.NA
    out["reason"] = df[reason_col].astype(str) if reason_col else pd.NA
    if yoy_col:
        ranges = [_parse_range(value, percent=True) for value in df[yoy_col]]
        out["net_profit_yoy_low"] = [item[0] for item in ranges]
        out["net_profit_yoy_high"] = [item[1] for item in ranges]
    else:
        out["net_profit_yoy_low"] = pd.NA
        out["net_profit_yoy_high"] = pd.NA
    if value_col:
        ranges = [_parse_range(value, percent=False) for value in df[value_col]]
        out["net_profit_low"] = [item[0] for item in ranges]
        out["net_profit_high"] = [item[1] for item in ranges]
    else:
        out["net_profit_low"] = pd.NA
        out["net_profit_high"] = pd.NA
    return out


def _normalize_flash(raw: pd.DataFrame, report_date: str) -> pd.DataFrame:
    out = _normalize_common(raw, report_date, "flash")
    if out.empty:
        return out
    df = raw.copy()
    columns = [str(column) for column in df.columns]
    revenue_col = _find_column(columns, "营业收入")
    revenue_yoy_col = _find_column(columns, "营业收入-同比", "营业收入同比")
    profit_col = _find_column(columns, "净利润")
    profit_yoy_col = _find_column(columns, "净利润-同比", "净利润同比")
    eps_col = _find_column(columns, "每股收益")
    roe_col = _find_column(columns, "净资产收益率")
    out["forecast_type"] = "flash"
    out["reason"] = pd.NA
    out["net_profit"] = [_parse_number(value) for value in df[profit_col]] if profit_col else pd.NA
    out["net_profit_yoy"] = (
        [_parse_number(value, percent=True) for value in df[profit_yoy_col]]
        if profit_yoy_col
        else pd.NA
    )
    out["revenue"] = [_parse_number(value) for value in df[revenue_col]] if revenue_col else pd.NA
    out["revenue_yoy"] = (
        [_parse_number(value, percent=True) for value in df[revenue_yoy_col]]
        if revenue_yoy_col
        else pd.NA
    )
    out["eps"] = [_parse_number(value) for value in df[eps_col]] if eps_col else pd.NA
    out["roe"] = [_parse_number(value, percent=True) for value in df[roe_col]] if roe_col else pd.NA
    out["net_profit_low"] = out["net_profit"]
    out["net_profit_high"] = out["net_profit"]
    out["net_profit_yoy_low"] = out["net_profit_yoy"]
    out["net_profit_yoy_high"] = out["net_profit_yoy"]
    return out


def _fetch_with_retry(kind: str, report_date: str) -> pd.DataFrame:
    ak = _import_akshare()
    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            if kind == "forecast":
                raw = ak.stock_yjyg_em(date=report_date)
                return _normalize_forecast(pd.DataFrame(raw), report_date)
            if kind == "flash":
                raw = ak.stock_yjkb_em(date=report_date)
                return _normalize_flash(pd.DataFrame(raw), report_date)
            raise ValueError(f"unsupported kind: {kind}")
        except Exception as exc:
            last_error = exc
            if attempt < RETRIES:
                time.sleep(SLEEP_SECONDS * attempt)
    raise RuntimeError(f"{kind} {report_date}: {last_error}")


def _write_atomically(path: os.PathLike[str] | str, df: pd.DataFrame) -> None:
    out_path = EARNINGS_EVENTS_DIR / Path(path).name
    tmp_path = out_path.with_name(f"{out_path.name}.{os.getpid()}.tmp")
    try:
        df.to_parquet(tmp_path)
        tmp_path.replace(out_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def main() -> None:
    started = time.time()
    report_dates = _quarter_dates(START_REPORT, END_REPORT)
    failures: list[dict[str, str]] = []
    frames: list[pd.DataFrame] = []
    print(
        f"Fetching EastMoney earnings events reports={len(report_dates)} "
        f"{START_REPORT}->{END_REPORT} skip_existing={SKIP_EXISTING}",
        flush=True,
    )
    for idx, report_date in enumerate(report_dates, start=1):
        for kind in ("forecast", "flash"):
            out_path = EARNINGS_EVENTS_DIR / f"{kind}_{report_date}.parquet"
            try:
                if SKIP_EXISTING and out_path.exists():
                    df = pd.read_parquet(out_path)
                else:
                    df = _fetch_with_retry(kind, report_date)
                    _write_atomically(out_path, df)
                    time.sleep(SLEEP_SECONDS)
                if not df.empty:
                    frames.append(df)
            except Exception as exc:
                failures.append({"kind": kind, "report_date": report_date, "error": str(exc)})
        if idx <= 3 or idx % 10 == 0:
            print(
                f"[{idx}/{len(report_dates)}] frames={len(frames)} failures={len(failures)}",
                flush=True,
            )
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if not combined.empty:
        combined = combined.dropna(subset=["code", "report_date"])
        combined = combined.drop_duplicates(
            ["code", "report_date", "event_type", "announcement_date"], keep="last"
        )
        combined = combined.sort_values(["announcement_date", "report_date", "code"])
        _write_atomically("earnings_events.parquet", combined)
    summary = {
        "timestamp": datetime.now().isoformat(),
        "source": "eastmoney_akshare",
        "start_report": START_REPORT,
        "end_report": END_REPORT,
        "report_dates": report_dates,
        "rows": int(len(combined)),
        "symbols": int(combined["code"].nunique()) if not combined.empty else 0,
        "failures": failures,
        "elapsed_s": round(time.time() - started, 1),
    }
    (EARNINGS_EVENTS_DIR / "fetch_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(
        f"Done rows={summary['rows']} symbols={summary['symbols']} failures={len(failures)}",
        flush=True,
    )
    if len(combined) < MIN_ROWS_TOTAL:
        raise RuntimeError(
            f"insufficient earnings event rows {len(combined)} < {MIN_ROWS_TOTAL}; "
            "provider output is not usable"
        )


if __name__ == "__main__":
    main()
