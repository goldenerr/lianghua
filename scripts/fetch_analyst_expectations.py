#!/usr/bin/env python3
"""Fetch analyst consensus, ratings, and revision-style signals.

This script intentionally stores both snapshot-style consensus data and dated
rating reports. Research code must only consume rows whose ``publish_date`` or
``asof_date`` is not later than the rebalance date.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pandas as pd
from _paths import ANALYST_EXPECTATIONS_DIR, STOCK_LIST

ANALYST_EXPECTATIONS_DIR.mkdir(parents=True, exist_ok=True)

RATING_START = os.getenv("QUANT_ANALYST_RATING_START", "20260529")
RATING_END = os.getenv("QUANT_ANALYST_RATING_END", RATING_START)
SLEEP_SECONDS = float(os.getenv("QUANT_ANALYST_SLEEP_SECONDS", "0.2"))
RETRIES = int(os.getenv("QUANT_ANALYST_RETRIES", "3"))
FETCH_THS_DETAILS = os.getenv("QUANT_ANALYST_FETCH_THS_DETAILS", "0") == "1"
MAX_THS_SYMBOLS = int(os.getenv("QUANT_ANALYST_MAX_THS_SYMBOLS", "50"))
SKIP_EXISTING = os.getenv("QUANT_ANALYST_SKIP_EXISTING", "1") == "1"

UNIT_MULTIPLIERS = {
    "万亿": 1_000_000_000_000.0,
    "亿元": 100_000_000.0,
    "亿": 100_000_000.0,
    "万元": 10_000.0,
    "万": 10_000.0,
}

RATING_SCORE = {
    "强烈推荐": 5.0,
    "推荐": 4.5,
    "买入": 4.0,
    "增持": 3.0,
    "谨慎推荐": 3.0,
    "中性": 2.0,
    "持有": 2.0,
    "减持": 1.0,
    "卖出": 0.0,
}

REVISION_SCORE = {
    "调高": 1.0,
    "上调": 1.0,
    "维持": 0.0,
    "首次": 0.0,
    "无": 0.0,
    "不变": 0.0,
    "调低": -1.0,
    "下调": -1.0,
}


def _import_akshare() -> Any:
    try:
        import akshare as ak
    except ModuleNotFoundError as exc:
        raise RuntimeError("akshare is required to fetch analyst expectation data") from exc
    return ak


def _load_codes() -> list[str]:
    raw = json.loads(STOCK_LIST.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise RuntimeError(f"stock list must be a JSON string array: {STOCK_LIST}")
    return [str(item).zfill(6) for item in cast(list[str], raw)]


def _write_atomically(path: Path, df: pd.DataFrame) -> None:
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        df.to_parquet(tmp_path)
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


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
    numeric = float(match.group(0)) * multiplier
    return numeric / 100.0 if percent else numeric


def _score_rating(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    for key, score in RATING_SCORE.items():
        if key in text:
            return score
    return None


def _score_revision(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    for key, score in REVISION_SCORE.items():
        if key in text:
            return score
    return None


def _fetch_with_retry(fetcher: Any, *args: Any, **kwargs: Any) -> pd.DataFrame:
    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            return pd.DataFrame(fetcher(*args, **kwargs))
        except Exception as exc:
            last_error = exc
            if attempt < RETRIES:
                time.sleep(SLEEP_SECONDS * attempt)
    raise RuntimeError(str(last_error))


def _normalize_profit_forecast(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()
    df = raw.copy()
    columns = [str(column) for column in df.columns]
    code_col = _find_column(columns, "代码", "证券代码")
    name_col = _find_column(columns, "名称", "证券简称")
    report_count_col = _find_column(columns, "研报数")
    if code_col is None:
        raise RuntimeError(f"profit forecast missing code column: {columns}")
    out = pd.DataFrame()
    out["code"] = df[code_col].astype(str).str.extract(r"(\d{6})", expand=False).str.zfill(6)
    out["name"] = df[name_col].astype(str) if name_col else pd.NA
    out["asof_date"] = pd.Timestamp(datetime.now().date())
    out["report_count_6m"] = (
        [_parse_number(value) for value in df[report_count_col]] if report_count_col else pd.NA
    )
    for rating_name in ("买入", "增持", "中性", "减持", "卖出"):
        source_col = _find_column(columns, f"机构投资评级(近六个月)-{rating_name}", rating_name)
        out[f"rating_count_{rating_name}"] = (
            [_parse_number(value) for value in df[source_col]] if source_col else pd.NA
        )
    forecast_columns = {
        "forecast_eps_2025": ("2025预测每股收益",),
        "forecast_eps_2026": ("2026预测每股收益",),
        "forecast_eps_2027": ("2027预测每股收益",),
        "forecast_np_2025": ("2025预测净利润",),
        "forecast_np_2026": ("2026预测净利润",),
        "forecast_np_2027": ("2027预测净利润",),
    }
    for target, patterns in forecast_columns.items():
        source_col = _find_column(columns, *patterns)
        out[target] = [_parse_number(value) for value in df[source_col]] if source_col else pd.NA
    out["source"] = "eastmoney_profit_forecast_akshare"
    out["fetched_at"] = datetime.now().isoformat()
    return out.dropna(subset=["code"]).drop_duplicates("code", keep="last")


def _normalize_cninfo_ratings(raw: pd.DataFrame, date: str) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()
    df = raw.copy()
    columns = [str(column) for column in df.columns]
    code_col = _find_column(columns, "证券代码", "代码")
    name_col = _find_column(columns, "证券简称", "简称")
    publish_col = _find_column(columns, "发布日期", "日期")
    institution_col = _find_column(columns, "研究机构简称", "机构")
    analyst_col = _find_column(columns, "研究员名称", "研究员")
    rating_col = _find_column(columns, "投资评级", "评级")
    revision_col = _find_column(columns, "评级变化", "变化")
    previous_col = _find_column(columns, "前一次投资评级", "前一次")
    target_low_col = _find_column(columns, "目标价格-下限", "目标价下限")
    target_high_col = _find_column(columns, "目标价格-上限", "目标价上限")
    if code_col is None:
        raise RuntimeError(f"CNInfo ratings {date}: missing code column")
    out = pd.DataFrame()
    out["code"] = df[code_col].astype(str).str.extract(r"(\d{6})", expand=False).str.zfill(6)
    out["name"] = df[name_col].astype(str) if name_col else pd.NA
    out["publish_date"] = (
        pd.to_datetime(df[publish_col], errors="coerce") if publish_col else pd.Timestamp(date)
    )
    out["institution"] = df[institution_col].astype(str) if institution_col else pd.NA
    out["analyst"] = df[analyst_col].astype(str) if analyst_col else pd.NA
    out["rating"] = df[rating_col].astype(str) if rating_col else pd.NA
    out["previous_rating"] = df[previous_col].astype(str) if previous_col else pd.NA
    out["rating_revision"] = df[revision_col].astype(str) if revision_col else pd.NA
    out["rating_score"] = [_score_rating(value) for value in out["rating"]]
    out["previous_rating_score"] = [_score_rating(value) for value in out["previous_rating"]]
    out["revision_score"] = [_score_revision(value) for value in out["rating_revision"]]
    out["target_price_low"] = (
        [_parse_number(value) for value in df[target_low_col]] if target_low_col else pd.NA
    )
    out["target_price_high"] = (
        [_parse_number(value) for value in df[target_high_col]] if target_high_col else pd.NA
    )
    out["source_date"] = pd.Timestamp(date)
    out["source"] = "cninfo_stock_rank_forecast_akshare"
    out["fetched_at"] = datetime.now().isoformat()
    return out.dropna(subset=["code", "publish_date"])


def _normalize_ths_detail(raw: pd.DataFrame, code: str, indicator: str) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()
    df = raw.copy()
    out = df.rename(columns={column: str(column) for column in df.columns})
    out.insert(0, "code", code)
    out.insert(1, "indicator", indicator)
    out["source"] = "ths_profit_forecast_detail_akshare"
    out["fetched_at"] = datetime.now().isoformat()
    return out


def _date_range(start: str, end: str) -> list[str]:
    dates = pd.bdate_range(pd.Timestamp(start), pd.Timestamp(end))
    return [date.strftime("%Y%m%d") for date in dates]


def fetch_profit_forecast(ak: Any) -> dict[str, Any]:
    path = ANALYST_EXPECTATIONS_DIR / "profit_forecast_em.parquet"
    if SKIP_EXISTING and path.exists():
        df = pd.read_parquet(path)
        return {"status": "skipped", "rows": len(df), "path": str(path)}
    raw = _fetch_with_retry(ak.stock_profit_forecast_em)
    df = _normalize_profit_forecast(raw)
    _write_atomically(path, df)
    return {"status": "ok", "rows": len(df), "path": str(path)}


def fetch_cninfo_ratings(ak: Any) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    frames: list[pd.DataFrame] = []
    for date in _date_range(RATING_START, RATING_END):
        path = ANALYST_EXPECTATIONS_DIR / f"cninfo_ratings_{date}.parquet"
        try:
            if SKIP_EXISTING and path.exists():
                df = pd.read_parquet(path)
                status = "skipped"
            else:
                raw = _fetch_with_retry(ak.stock_rank_forecast_cninfo, date=date)
                df = _normalize_cninfo_ratings(raw, date)
                _write_atomically(path, df)
                status = "ok"
                time.sleep(SLEEP_SECONDS)
            if not df.empty:
                frames.append(df)
            rows.append({"date": date, "status": status, "rows": len(df)})
        except Exception as exc:
            failures.append({"date": date, "error": str(exc)})
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if not combined.empty:
        combined = combined.drop_duplicates(
            ["code", "publish_date", "institution", "analyst", "rating"], keep="last"
        )
    _write_atomically(ANALYST_EXPECTATIONS_DIR / "analyst_ratings_cninfo.parquet", combined)
    return rows, failures


def fetch_ths_details(ak: Any) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if not FETCH_THS_DETAILS:
        return [], []
    detail_dir = ANALYST_EXPECTATIONS_DIR / "ths_details"
    detail_dir.mkdir(parents=True, exist_ok=True)
    codes = _load_codes()[:MAX_THS_SYMBOLS]
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    indicators = ("预测年报每股收益", "预测年报净利润", "业绩预测详表-机构")
    for code in codes:
        for indicator in indicators:
            safe_indicator = re.sub(r"\W+", "_", indicator)
            path = detail_dir / f"{code}_{safe_indicator}.parquet"
            try:
                if SKIP_EXISTING and path.exists():
                    df = pd.read_parquet(path)
                    status = "skipped"
                else:
                    raw = _fetch_with_retry(
                        ak.stock_profit_forecast_ths, symbol=code, indicator=indicator
                    )
                    df = _normalize_ths_detail(raw, code, indicator)
                    _write_atomically(path, df)
                    status = "ok"
                    time.sleep(SLEEP_SECONDS)
                rows.append({"code": code, "indicator": indicator, "status": status, "rows": len(df)})
            except Exception as exc:
                failures.append({"code": code, "indicator": indicator, "error": str(exc)})
    return rows, failures


def main() -> None:
    started = time.time()
    ak = _import_akshare()
    failures: list[dict[str, str]] = []
    print(
        "Fetching analyst expectations: "
        f"ratings={RATING_START}->{RATING_END} ths_details={FETCH_THS_DETAILS}",
        flush=True,
    )
    profit_summary = fetch_profit_forecast(ak)
    rating_rows, rating_failures = fetch_cninfo_ratings(ak)
    ths_rows, ths_failures = fetch_ths_details(ak)
    failures.extend(rating_failures)
    failures.extend(ths_failures)
    summary = {
        "timestamp": datetime.now().isoformat(),
        "source": [
            "eastmoney_profit_forecast_akshare",
            "cninfo_stock_rank_forecast_akshare",
            "ths_profit_forecast_detail_akshare" if FETCH_THS_DETAILS else "ths_detail_disabled",
        ],
        "point_in_time_warning": (
            "profit_forecast_em is a current snapshot; use only for as-of research after "
            "snapshot date or replace with a paid PIT consensus provider in production."
        ),
        "profit_forecast": profit_summary,
        "ratings": rating_rows,
        "ths_details": ths_rows,
        "failed": len(failures),
        "failures": failures,
        "elapsed_s": round(time.time() - started, 1),
    }
    (ANALYST_EXPECTATIONS_DIR / "fetch_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(
        f"Done analyst expectations: profit_rows={profit_summary.get('rows')} "
        f"rating_days={len(rating_rows)} failures={len(failures)}",
        flush=True,
    )
    if failures:
        raise RuntimeError(f"analyst expectations fetch had {len(failures)} failures")


if __name__ == "__main__":
    main()
