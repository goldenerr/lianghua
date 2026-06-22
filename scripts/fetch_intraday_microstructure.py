#!/usr/bin/env python3
"""Fetch intraday bars and derive liquidity/reversal microstructure features.

Sina minute bars are used as the public fallback because EastMoney minute
requests were unstable in provider probes. The endpoint usually exposes a
recent rolling window, so this is a daily archival source rather than a full
historical minute-data replacement.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
from _paths import INTRADAY_DIR, STOCK_LIST

INTRADAY_DIR.mkdir(parents=True, exist_ok=True)
BARS_DIR = INTRADAY_DIR / "bars_5m"
FEATURES_DIR = INTRADAY_DIR / "features"
BARS_DIR.mkdir(parents=True, exist_ok=True)
FEATURES_DIR.mkdir(parents=True, exist_ok=True)

PERIOD = os.getenv("QUANT_INTRADAY_PERIOD", "5")
MAX_SYMBOLS = int(os.getenv("QUANT_INTRADAY_MAX_SYMBOLS", "50"))
SYMBOL_OFFSET = int(os.getenv("QUANT_INTRADAY_SYMBOL_OFFSET", "0"))
MAX_WORKERS = max(1, int(os.getenv("QUANT_INTRADAY_MAX_WORKERS", "1")))
SLEEP_SECONDS = float(os.getenv("QUANT_INTRADAY_SLEEP_SECONDS", "0.2"))
RETRIES = int(os.getenv("QUANT_INTRADAY_RETRIES", "3"))
SKIP_EXISTING = os.getenv("QUANT_INTRADAY_SKIP_EXISTING", "1") == "1"
COMBINE_ALL_EXISTING = os.getenv("QUANT_INTRADAY_COMBINE_ALL_EXISTING", "1") == "1"
ALLOW_PARTIAL = os.getenv("QUANT_INTRADAY_ALLOW_PARTIAL", "1") == "1"


def _import_akshare() -> Any:
    try:
        import akshare as ak
    except ModuleNotFoundError as exc:
        raise RuntimeError("akshare is required to fetch intraday microstructure data") from exc
    return ak


def _load_codes() -> list[str]:
    raw = json.loads(STOCK_LIST.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise RuntimeError(f"stock list must be a JSON string array: {STOCK_LIST}")
    return [str(item).zfill(6) for item in cast(list[str], raw)]


def _selected_codes() -> list[str]:
    codes = _load_codes()
    if SYMBOL_OFFSET < 0:
        raise ValueError("QUANT_INTRADAY_SYMBOL_OFFSET must be >= 0")
    if MAX_SYMBOLS <= 0:
        return codes[SYMBOL_OFFSET:]
    return codes[SYMBOL_OFFSET : SYMBOL_OFFSET + MAX_SYMBOLS]


def _write_atomically(path: Path, df: pd.DataFrame) -> None:
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        df.to_parquet(tmp_path)
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _market_symbol(code: str) -> str:
    if code.startswith(("5", "6", "9")):
        return f"sh{code}"
    if code.startswith(("0", "1", "2", "3")):
        return f"sz{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    return code


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


def _load_existing_feature_frames() -> list[pd.DataFrame]:
    frames: list[pd.DataFrame] = []
    for path in sorted(FEATURES_DIR.glob("*.parquet")):
        df = pd.read_parquet(path)
        if not df.empty:
            frames.append(df)
    return frames


def _normalize_bars(raw: pd.DataFrame, code: str) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()
    df = raw.rename(
        columns={
            "day": "timestamp",
            "日期": "timestamp",
            "时间": "timestamp",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "成交额": "amount",
        }
    ).copy()
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"{code}: missing intraday columns {sorted(missing)} from {list(raw.columns)}")
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume", "amount"):
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    if "amount" not in df.columns:
        df["amount"] = df["close"] * df["volume"]
    df = df.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    df.insert(0, "code", code)
    df["source"] = "sina_stock_zh_a_minute_akshare"
    df["fetched_at"] = datetime.now().isoformat()
    return df


def _safe_divide(numerator: float, denominator: float) -> float:
    if not np.isfinite(numerator) or not np.isfinite(denominator) or denominator == 0:
        return float("nan")
    return float(numerator / denominator)


def _derive_daily_features(bars: pd.DataFrame, code: str) -> pd.DataFrame:
    if bars.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    working = bars.copy()
    working["date"] = working["timestamp"].dt.normalize()
    for date, group in working.groupby("date", sort=True):
        group = group.sort_values("timestamp")
        close = group["close"].astype(float)
        open_ = group["open"].astype(float)
        high = group["high"].astype(float)
        low = group["low"].astype(float)
        volume = group["volume"].astype(float)
        amount = group["amount"].astype(float)
        returns = close.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
        total_volume = float(volume.sum())
        total_amount = float(amount.sum())
        first_open = float(open_.iloc[0])
        last_close = float(close.iloc[-1])
        day_high = float(high.max())
        day_low = float(low.min())
        vwap = _safe_divide(total_amount, total_volume)
        first_hour = group.head(min(12, len(group)))
        last_hour = group.tail(min(12, len(group)))
        first_hour_volume = float(first_hour["volume"].sum())
        last_hour_volume = float(last_hour["volume"].sum())
        first_hour_return = _safe_divide(float(first_hour["close"].iloc[-1]), first_open) - 1.0
        close_pressure = _safe_divide(last_close, vwap) - 1.0
        range_position = _safe_divide(last_close - day_low, day_high - day_low)
        realized_vol = float(returns.std(ddof=0) * np.sqrt(len(returns))) if len(returns) else np.nan
        amihud = _safe_divide(float(returns.abs().sum()), max(total_amount, 1.0))
        rows.append(
            {
                "code": code,
                "date": pd.Timestamp(date),
                "bars": len(group),
                "intraday_return": _safe_divide(last_close, first_open) - 1.0,
                "intraday_reversal_alpha": -first_hour_return,
                "first_hour_return": first_hour_return,
                "realized_vol_5m": realized_vol,
                "amihud_5m": amihud,
                "vwap": vwap,
                "close_vs_vwap": close_pressure,
                "range_position": range_position,
                "first_hour_volume_share": _safe_divide(first_hour_volume, total_volume),
                "last_hour_volume_share": _safe_divide(last_hour_volume, total_volume),
                "turnover_amount": total_amount,
                "turnover_volume": total_volume,
                "source": "sina_stock_zh_a_minute_akshare",
                "fetched_at": datetime.now().isoformat(),
            }
        )
    return pd.DataFrame(rows)


def fetch_one(ak: Any, code: str) -> dict[str, Any]:
    bars_path = BARS_DIR / f"{code}.parquet"
    features_path = FEATURES_DIR / f"{code}.parquet"
    if SKIP_EXISTING and bars_path.exists() and features_path.exists():
        bars = pd.read_parquet(bars_path)
        features = pd.read_parquet(features_path)
        return {"code": code, "status": "skipped", "bars": len(bars), "feature_rows": len(features)}
    symbol = _market_symbol(code)
    raw = _fetch_with_retry(ak.stock_zh_a_minute, symbol=symbol, period=PERIOD, adjust="")
    bars = _normalize_bars(raw, code)
    if bars.empty:
        raise RuntimeError(f"{code}: empty normalized intraday bars")
    features = _derive_daily_features(bars, code)
    _write_atomically(bars_path, bars)
    _write_atomically(features_path, features)
    return {"code": code, "status": "ok", "bars": len(bars), "feature_rows": len(features)}


def fetch_one_with_import(code: str) -> dict[str, Any]:
    ak = _import_akshare()
    result = fetch_one(ak, code)
    if result.get("status") == "ok":
        time.sleep(SLEEP_SECONDS)
    return result


def main() -> None:
    started = time.time()
    ak = _import_akshare()
    codes = _selected_codes()
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    frames: list[pd.DataFrame] = []
    print(
        f"Fetching intraday microstructure: symbol_offset={SYMBOL_OFFSET} "
        f"symbols={len(codes)} max_symbols={MAX_SYMBOLS} period={PERIOD} "
        f"workers={MAX_WORKERS} combine_all_existing={COMBINE_ALL_EXISTING} "
        f"allow_partial={ALLOW_PARTIAL}",
        flush=True,
    )
    if MAX_WORKERS == 1 or len(codes) <= 1:
        for idx, code in enumerate(codes, start=1):
            try:
                result = fetch_one(ak, code)
                rows.append(result)
                feature_path = FEATURES_DIR / f"{code}.parquet"
                if feature_path.exists():
                    features = pd.read_parquet(feature_path)
                    if not features.empty:
                        frames.append(features)
                time.sleep(SLEEP_SECONDS)
            except Exception as exc:
                failures.append({"code": code, "error": str(exc)})
            if idx <= 5 or idx % 25 == 0:
                print(f"[{idx}/{len(codes)}] ok={len(rows)} fail={len(failures)}", flush=True)
    else:
        order = {code: idx for idx, code in enumerate(codes)}
        completed = 0
        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(fetch_one_with_import, code): code for code in codes}
            for future in as_completed(futures):
                code = futures[future]
                completed += 1
                try:
                    result = future.result()
                    rows.append(result)
                    feature_path = FEATURES_DIR / f"{code}.parquet"
                    if feature_path.exists():
                        features = pd.read_parquet(feature_path)
                        if not features.empty:
                            frames.append(features)
                except Exception as exc:
                    failures.append({"code": code, "error": str(exc)})
                if completed <= 5 or completed % 25 == 0:
                    print(
                        f"[{completed}/{len(codes)}] ok={len(rows)} fail={len(failures)} "
                        f"workers={MAX_WORKERS}",
                        flush=True,
                    )
        rows.sort(key=lambda item: order.get(str(item.get("code", "")), len(order)))
        failures.sort(key=lambda item: order.get(str(item.get("code", "")), len(order)))
    if COMBINE_ALL_EXISTING:
        frames = _load_existing_feature_frames()
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    _write_atomically(INTRADAY_DIR / "microstructure_features.parquet", combined)
    summary = {
        "timestamp": datetime.now().isoformat(),
        "source": "sina_stock_zh_a_minute_akshare",
        "period": PERIOD,
        "provider_window_warning": (
            "Sina minute bars expose a recent rolling window; run this fetcher daily to build "
            "a true historical intraday archive before using it in production research."
        ),
        "symbols_requested": len(codes),
        "symbol_offset": SYMBOL_OFFSET,
        "max_symbols": MAX_SYMBOLS,
        "max_workers": MAX_WORKERS,
        "combine_all_existing": COMBINE_ALL_EXISTING,
        "success": len(rows),
        "failed": len(failures),
        "results": rows,
        "failures": failures,
        "combined_feature_rows": len(combined),
        "elapsed_s": round(time.time() - started, 1),
    }
    (INTRADAY_DIR / "fetch_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(
        f"Done intraday microstructure: success={len(rows)} failed={len(failures)} "
        f"feature_rows={len(combined)}",
        flush=True,
    )
    if failures and not ALLOW_PARTIAL:
        raise RuntimeError(f"intraday fetch had {len(failures)} failures")


if __name__ == "__main__":
    main()
