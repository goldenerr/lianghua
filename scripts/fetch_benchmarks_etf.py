#!/usr/bin/env python3
"""Fetch benchmark index and ETF bars for portfolio overlay research.

Default assets are China equity benchmarks plus liquid ETF proxies for cash and
gold. The script is incremental: existing symbols that already reach the target
end date are skipped unless QUANT_BENCHMARK_FORCE_FULL=1 is set.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from _paths import PROJECT_DIR

BENCHMARK_DIR = Path(os.getenv("QUANT_BENCHMARK_DIR", PROJECT_DIR / "data" / "benchmarks"))
BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)

START_DATE = os.getenv("QUANT_BENCHMARK_START", "20000101")
END_DATE = os.getenv("QUANT_BENCHMARK_END", "20260529")
ADJUST = os.getenv("QUANT_BENCHMARK_ADJUST", "qfq")
FORCE_FULL = os.getenv("QUANT_BENCHMARK_FORCE_FULL", "0").strip().lower() in {
    "1",
    "true",
    "yes",
}

DEFAULT_ASSETS: list[dict[str, str]] = [
    {"id": "idx_000300", "type": "index", "symbol": "000300", "name": "沪深300", "tencent": "sh000300"},
    {"id": "idx_000905", "type": "index", "symbol": "000905", "name": "中证500", "tencent": "sh000905"},
    {"id": "idx_000852", "type": "index", "symbol": "000852", "name": "中证1000", "tencent": "sh000852"},
    {"id": "idx_399006", "type": "index", "symbol": "399006", "name": "创业板指", "tencent": "sz399006"},
    {"id": "etf_510300", "type": "etf", "symbol": "510300", "name": "沪深300ETF", "tencent": "sh510300"},
    {"id": "etf_510500", "type": "etf", "symbol": "510500", "name": "中证500ETF", "tencent": "sh510500"},
    {"id": "etf_512100", "type": "etf", "symbol": "512100", "name": "中证1000ETF", "tencent": "sh512100"},
    {"id": "etf_159915", "type": "etf", "symbol": "159915", "name": "创业板ETF", "tencent": "sz159915"},
    {"id": "etf_588000", "type": "etf", "symbol": "588000", "name": "科创50ETF", "tencent": "sh588000"},
    {"id": "etf_511880", "type": "etf", "symbol": "511880", "name": "银华日利货币ETF", "tencent": "sh511880"},
    {"id": "etf_518880", "type": "etf", "symbol": "518880", "name": "黄金ETF", "tencent": "sh518880"},
]

COLUMN_MAP = {
    "日期": "date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
    "振幅": "amplitude",
    "涨跌幅": "pct_change",
    "涨跌额": "change",
    "换手率": "turnover",
}
REQUIRED_COLUMNS = ["open", "high", "low", "close", "volume", "amount"]


def _import_akshare() -> Any:
    try:
        import akshare as ak
    except ModuleNotFoundError as exc:
        raise RuntimeError("akshare is required to fetch benchmark and ETF data") from exc
    return ak


def _asset_path(asset_id: str) -> Path:
    return BENCHMARK_DIR / f"{asset_id}.parquet"


def _is_current(asset_id: str, end_ts: pd.Timestamp) -> bool:
    if FORCE_FULL:
        return False
    path = _asset_path(asset_id)
    if not path.exists():
        return False
    try:
        df = pd.read_parquet(path)
    except Exception:
        return False
    if df.empty:
        return False
    return pd.Timestamp(df.index.max()).normalize() >= end_ts


def _normalize(raw: pd.DataFrame) -> pd.DataFrame:
    df = pd.DataFrame(raw).rename(columns=COLUMN_MAP)
    missing = [column for column in [*REQUIRED_COLUMNS, "date"] if column not in df.columns]
    if missing:
        raise RuntimeError(f"missing benchmark columns {missing}; provider columns={list(df.columns)}")
    df["date"] = pd.to_datetime(df["date"], errors="raise")
    df = df.set_index("date").sort_index()
    for column in REQUIRED_COLUMNS:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df[REQUIRED_COLUMNS].dropna(subset=["open", "high", "low", "close"])
    if df.empty:
        raise RuntimeError("empty normalized benchmark data")
    return df.astype(float)


def _to_tencent_date(date_str: str) -> str:
    return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"


def _normalize_tencent(rows: list[list[Any]]) -> pd.DataFrame:
    parsed: list[dict[str, Any]] = []
    for row in rows:
        if len(row) < 6:
            continue
        close = float(row[2])
        volume = float(row[5])
        amount = float(row[6]) if len(row) > 6 and row[6] not in {"", None} else close * volume
        parsed.append(
            {
                "date": row[0],
                "open": row[1],
                "close": row[2],
                "high": row[3],
                "low": row[4],
                "volume": volume,
                "amount": amount,
            }
        )
    return _normalize(pd.DataFrame(parsed))


def _fetch_tencent(asset: dict[str, str]) -> pd.DataFrame:
    symbol = asset["tencent"]
    fq = "qfq" if ADJUST else ""
    start_dt = datetime.strptime(START_DATE, "%Y%m%d").date()
    cursor_end = datetime.strptime(END_DATE, "%Y%m%d").date()
    all_rows: list[list[Any]] = []
    seen_dates: set[str] = set()
    while cursor_end >= start_dt:
        params = urllib.parse.quote(
            f"{symbol},day,{_to_tencent_date(START_DATE)},{cursor_end.isoformat()},640,{fq}"
        )
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={params}"
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except Exception as exc:
                last_error = exc
                if attempt == 2:
                    raise
                time.sleep(1.0 + attempt)
        else:
            raise RuntimeError(f"Tencent request failed: {last_error}")
        data_node = payload.get("data", {})
        if isinstance(data_node, list):
            node: Any = data_node
        else:
            node = data_node.get(symbol, {})
        if isinstance(node, list):
            rows = node
        else:
            rows = node.get("qfqday") or node.get("hfqday") or node.get("day") or []
        if not rows:
            break
        for row in rows:
            if row and row[0] not in seen_dates:
                seen_dates.add(row[0])
                all_rows.append(row)
        first_date = datetime.strptime(rows[0][0], "%Y-%m-%d").date()
        if first_date <= start_dt or len(rows) < 640:
            break
        cursor_end = first_date - timedelta(days=1)
    if not all_rows:
        raise RuntimeError("empty Tencent kline response")
    return _normalize_tencent(all_rows)


def _fetch_one(ak: Any, asset: dict[str, str]) -> pd.DataFrame:
    try:
        return _fetch_tencent(asset)
    except Exception as tencent_exc:
        print(f"  tencent fallback needed for {asset['id']}: {tencent_exc}", flush=True)
    if asset["type"] == "index":
        raw = ak.index_zh_a_hist(
            symbol=asset["symbol"],
            period="daily",
            start_date=START_DATE,
            end_date=END_DATE,
        )
    elif asset["type"] == "etf":
        raw = ak.fund_etf_hist_em(
            symbol=asset["symbol"],
            period="daily",
            start_date=START_DATE,
            end_date=END_DATE,
            adjust=ADJUST,
        )
    else:
        raise RuntimeError(f"unsupported benchmark type: {asset['type']}")
    return _normalize(pd.DataFrame(raw))


def _write_atomically(asset_id: str, df: pd.DataFrame) -> None:
    path = _asset_path(asset_id)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        df.to_parquet(tmp_path)
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def main() -> None:
    end_ts = pd.Timestamp(datetime.strptime(END_DATE, "%Y%m%d")).normalize()
    ak = _import_akshare()
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for asset in DEFAULT_ASSETS:
        if _is_current(asset["id"], end_ts):
            print(f"skip current {asset['id']} {asset['name']}", flush=True)
            rows.append({"id": asset["id"], "status": "skipped"})
            continue
        try:
            df = _fetch_one(ak, asset)
            _write_atomically(asset["id"], df)
            print(
                f"ok {asset['id']} {asset['name']} rows={len(df)} "
                f"{df.index.min().date()}->{df.index.max().date()}",
                flush=True,
            )
            rows.append(
                {
                    "id": asset["id"],
                    "status": "ok",
                    "rows": len(df),
                    "start": str(df.index.min().date()),
                    "end": str(df.index.max().date()),
                }
            )
        except Exception as exc:
            print(f"fail {asset['id']} {asset['name']}: {exc}", flush=True)
            failures.append({"id": asset["id"], "error": str(exc)})
    summary = {
        "ts": datetime.now().isoformat(),
        "start": START_DATE,
        "end": END_DATE,
        "adjust": ADJUST,
        "total": len(DEFAULT_ASSETS),
        "success": sum(1 for row in rows if row["status"] == "ok"),
        "skipped": sum(1 for row in rows if row["status"] == "skipped"),
        "failed": len(failures),
        "assets": rows,
        "failures": failures,
    }
    (BENCHMARK_DIR / "fetch_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if failures:
        raise RuntimeError(f"benchmark fetch had {len(failures)} failures")


if __name__ == "__main__":
    main()
