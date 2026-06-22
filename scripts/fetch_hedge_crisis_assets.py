#!/usr/bin/env python3
"""Fetch hedge execution proxies and independent crisis-alpha sleeve assets.

The output separates tradable futures hedge instruments from ETF crisis-alpha
proxies. Production live trading still requires broker/exchange-backed
availability, margin, borrow and position-provider evidence before any hedge is
allowed to execute.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from _paths import HEDGE_ASSETS_DIR

HEDGE_ASSETS_DIR.mkdir(parents=True, exist_ok=True)

START_DATE = os.getenv("QUANT_HEDGE_ASSET_START", "20000101")
END_DATE = os.getenv("QUANT_HEDGE_ASSET_END", "20260529")
ADJUST = os.getenv("QUANT_HEDGE_ETF_ADJUST", "qfq")
SLEEP_SECONDS = float(os.getenv("QUANT_HEDGE_ASSET_SLEEP_SECONDS", "0.2"))
RETRIES = int(os.getenv("QUANT_HEDGE_ASSET_RETRIES", "3"))
SKIP_EXISTING = os.getenv("QUANT_HEDGE_ASSET_SKIP_EXISTING", "1") == "1"
ALLOW_PARTIAL = os.getenv("QUANT_HEDGE_ASSET_ALLOW_PARTIAL", "1") == "1"

DEFAULT_FUTURES: list[dict[str, str]] = [
    {"id": "fut_IF0", "symbol": "IF0", "name": "沪深300股指期货连续", "role": "beta_hedge_csi300"},
    {"id": "fut_IH0", "symbol": "IH0", "name": "上证50股指期货连续", "role": "large_cap_hedge"},
    {"id": "fut_IC0", "symbol": "IC0", "name": "中证500股指期货连续", "role": "mid_cap_hedge"},
    {"id": "fut_IM0", "symbol": "IM0", "name": "中证1000股指期货连续", "role": "small_cap_hedge"},
]

DEFAULT_ETFS: list[dict[str, str]] = [
    {"id": "etf_511880", "symbol": "511880", "name": "银华日利货币ETF", "role": "cash_defensive"},
    {"id": "etf_511010", "symbol": "511010", "name": "国债ETF", "role": "bond_defensive"},
    {"id": "etf_511260", "symbol": "511260", "name": "十年国债ETF", "role": "duration_defensive"},
    {"id": "etf_518880", "symbol": "518880", "name": "黄金ETF", "role": "gold_crisis_alpha"},
    {"id": "etf_159985", "symbol": "159985", "name": "豆粕ETF", "role": "commodity_inflation"},
    {"id": "etf_159980", "symbol": "159980", "name": "有色ETF", "role": "commodity_beta"},
    {"id": "etf_513100", "symbol": "513100", "name": "纳指ETF", "role": "global_equity_trend"},
    {"id": "etf_513500", "symbol": "513500", "name": "标普500ETF", "role": "global_equity_trend"},
    {"id": "etf_159920", "symbol": "159920", "name": "恒生ETF", "role": "hk_equity_trend"},
]

ETF_COLUMN_MAP = {
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

FUTURES_COLUMN_MAP = {
    "日期": "date",
    "开盘价": "open",
    "最高价": "high",
    "最低价": "low",
    "收盘价": "close",
    "成交量": "volume",
    "持仓量": "open_interest",
    "动态结算价": "settlement",
}

REQUIRED_OHLCV = ("open", "high", "low", "close", "volume")


def _import_akshare() -> Any:
    try:
        import akshare as ak
    except ModuleNotFoundError as exc:
        raise RuntimeError("akshare is required to fetch hedge and crisis assets") from exc
    return ak


def _write_atomically(path: Path, df: pd.DataFrame) -> None:
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        df.to_parquet(tmp_path)
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


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


def _normalize_ohlcv(raw: pd.DataFrame, column_map: dict[str, str], asset: dict[str, str]) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()
    df = raw.rename(columns=column_map).copy()
    missing = [column for column in ("date", *REQUIRED_OHLCV) if column not in df.columns]
    if missing:
        raise RuntimeError(f"{asset['id']}: missing columns {missing}; provider columns={list(raw.columns)}")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for column in (*REQUIRED_OHLCV, "amount", "open_interest", "settlement"):
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=["date", "open", "high", "low", "close"]).sort_values("date")
    df = df.drop_duplicates("date", keep="last").set_index("date")
    if df.empty:
        raise RuntimeError(f"{asset['id']}: empty normalized asset data")
    df["asset_id"] = asset["id"]
    df["symbol"] = asset["symbol"]
    df["name"] = asset["name"]
    df["role"] = asset["role"]
    df["source"] = asset.get("source", "akshare")
    df["fetched_at"] = datetime.now().isoformat()
    return df


def _is_current(path: Path) -> bool:
    if not SKIP_EXISTING or not path.exists():
        return False
    try:
        df = pd.read_parquet(path)
    except Exception:
        return False
    if df.empty:
        return False
    return pd.Timestamp(df.index.max()).normalize() >= pd.Timestamp(END_DATE).normalize()


def fetch_futures(ak: Any) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[pd.DataFrame]]:
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    frames: list[pd.DataFrame] = []
    for asset in DEFAULT_FUTURES:
        path = HEDGE_ASSETS_DIR / f"{asset['id']}.parquet"
        try:
            if _is_current(path):
                df = pd.read_parquet(path)
                status = "skipped"
            else:
                raw = _fetch_with_retry(
                    ak.futures_main_sina,
                    symbol=asset["symbol"],
                    start_date=START_DATE,
                    end_date=END_DATE,
                )
                asset_with_source = {**asset, "source": "sina_futures_main_akshare"}
                df = _normalize_ohlcv(raw, FUTURES_COLUMN_MAP, asset_with_source)
                _write_atomically(path, df)
                status = "ok"
                time.sleep(SLEEP_SECONDS)
            frames.append(df)
            rows.append(
                {
                    "id": asset["id"],
                    "symbol": asset["symbol"],
                    "status": status,
                    "rows": len(df),
                    "start": str(pd.Timestamp(df.index.min()).date()),
                    "end": str(pd.Timestamp(df.index.max()).date()),
                }
            )
        except Exception as exc:
            failures.append({"id": asset["id"], "symbol": asset["symbol"], "error": str(exc)})
    return rows, failures, frames


def fetch_etfs(ak: Any) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[pd.DataFrame]]:
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    frames: list[pd.DataFrame] = []
    for asset in DEFAULT_ETFS:
        path = HEDGE_ASSETS_DIR / f"{asset['id']}.parquet"
        try:
            if _is_current(path):
                df = pd.read_parquet(path)
                status = "skipped"
            else:
                raw = _fetch_with_retry(
                    ak.fund_etf_hist_em,
                    symbol=asset["symbol"],
                    period="daily",
                    start_date=START_DATE,
                    end_date=END_DATE,
                    adjust=ADJUST,
                )
                asset_with_source = {**asset, "source": "eastmoney_fund_etf_hist_akshare"}
                df = _normalize_ohlcv(raw, ETF_COLUMN_MAP, asset_with_source)
                _write_atomically(path, df)
                status = "ok"
                time.sleep(SLEEP_SECONDS)
            frames.append(df)
            rows.append(
                {
                    "id": asset["id"],
                    "symbol": asset["symbol"],
                    "status": status,
                    "rows": len(df),
                    "start": str(pd.Timestamp(df.index.min()).date()),
                    "end": str(pd.Timestamp(df.index.max()).date()),
                }
            )
        except Exception as exc:
            failures.append({"id": asset["id"], "symbol": asset["symbol"], "error": str(exc)})
    return rows, failures, frames


def main() -> None:
    started = time.time()
    ak = _import_akshare()
    print(
        f"Fetching hedge/crisis assets: {START_DATE}->{END_DATE} adjust={ADJUST} "
        f"allow_partial={ALLOW_PARTIAL}",
        flush=True,
    )
    future_rows, future_failures, future_frames = fetch_futures(ak)
    etf_rows, etf_failures, etf_frames = fetch_etfs(ak)
    failures = [*future_failures, *etf_failures]
    frames = [*future_frames, *etf_frames]
    combined = pd.concat(frames, axis=0).sort_index() if frames else pd.DataFrame()
    if not combined.empty:
        _write_atomically(HEDGE_ASSETS_DIR / "hedge_crisis_assets.parquet", combined)
    metadata = {
        "timestamp": datetime.now().isoformat(),
        "source": ["sina_futures_main_akshare", "eastmoney_fund_etf_hist_akshare"],
        "production_execution_warning": (
            "These are research/execution-proxy bars. Production market-neutral or crisis-alpha "
            "execution still requires broker/exchange-backed tradability, margin, borrow/short, "
            "account binding and position-provider evidence."
        ),
        "futures": future_rows,
        "etfs": etf_rows,
        "failed": len(failures),
        "failures": failures,
        "combined_rows": len(combined),
        "elapsed_s": round(time.time() - started, 1),
    }
    (HEDGE_ASSETS_DIR / "fetch_summary.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(
        f"Done hedge/crisis assets: futures={len(future_rows)} etfs={len(etf_rows)} "
        f"failures={len(failures)} rows={len(combined)}",
        flush=True,
    )
    if failures and not ALLOW_PARTIAL:
        raise RuntimeError(f"hedge/crisis asset fetch had {len(failures)} failures")


if __name__ == "__main__":
    main()
