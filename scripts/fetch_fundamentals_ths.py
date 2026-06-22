#!/usr/bin/env python3
"""Fetch point-in-time-friendly THS financial abstracts for A-share symbols.

The output is one parquet per stock under ``data/fundamentals``. Values are
normalized from Chinese text units and percentages while retaining report dates.
Research scripts must still apply an announcement/report lag before using these
features to avoid look-ahead bias.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from multiprocessing import Process, Queue
from multiprocessing.queues import Queue as QueueType
from typing import Any, cast

import pandas as pd
from _paths import FUNDAMENTALS_DIR, STOCK_LIST

FUNDAMENTALS_DIR.mkdir(parents=True, exist_ok=True)

MAX_WORKERS = max(1, int(os.getenv("QUANT_FUNDAMENTALS_MAX_WORKERS", "4")))
SYMBOL_TIMEOUT_SECONDS = float(os.getenv("QUANT_FUNDAMENTALS_SYMBOL_TIMEOUT_SECONDS", "60"))
MIN_ROWS = int(os.getenv("QUANT_FUNDAMENTALS_MIN_ROWS", "4"))
SKIP_EXISTING = os.getenv("QUANT_FUNDAMENTALS_SKIP_EXISTING", "1") == "1"

COLUMN_MAP = {
    "报告期": "report_date",
    "净利润": "net_profit",
    "净利润同比增长率": "net_profit_yoy",
    "扣非净利润": "net_profit_ex_nonrecurring",
    "扣非净利润同比增长率": "net_profit_ex_nonrecurring_yoy",
    "营业总收入": "revenue",
    "营业总收入同比增长率": "revenue_yoy",
    "基本每股收益": "eps",
    "每股净资产": "book_value_per_share",
    "每股资本公积金": "capital_reserve_per_share",
    "每股未分配利润": "retained_earnings_per_share",
    "每股经营现金流": "operating_cash_flow_per_share",
    "销售净利率": "net_margin",
    "销售毛利率": "gross_margin",
    "净资产收益率": "roe",
    "净资产收益率-摊薄": "roe_diluted",
    "营业周期": "operating_cycle_days",
    "存货周转率": "inventory_turnover",
    "存货周转天数": "inventory_turnover_days",
    "应收账款周转天数": "receivable_turnover_days",
    "流动比率": "current_ratio",
    "速动比率": "quick_ratio",
    "保守速动比率": "conservative_quick_ratio",
    "产权比率": "equity_multiplier_ratio",
    "资产负债率": "debt_asset_ratio",
}

PERCENT_COLUMNS = {
    "net_profit_yoy",
    "net_profit_ex_nonrecurring_yoy",
    "revenue_yoy",
    "net_margin",
    "gross_margin",
    "roe",
    "roe_diluted",
    "debt_asset_ratio",
}

UNIT_MULTIPLIERS = {
    "万亿": 1_000_000_000_000.0,
    "亿": 100_000_000.0,
    "万": 10_000.0,
}


def _import_akshare() -> Any:
    try:
        import akshare as ak
    except ModuleNotFoundError as exc:
        raise RuntimeError("akshare is required to fetch THS fundamentals") from exc
    return ak


def load_codes() -> list[str]:
    raw = json.loads(STOCK_LIST.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise RuntimeError(f"stock list must be a JSON string array: {STOCK_LIST}")
    return cast(list[str], raw)


def _parse_value(value: Any, *, percent: bool = False) -> float | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "--", "False", "false", "None", "nan"}:
        return None
    multiplier = 1.0
    if text.endswith("%"):
        percent = True
        text = text[:-1]
    for unit, unit_multiplier in UNIT_MULTIPLIERS.items():
        if text.endswith(unit):
            multiplier = unit_multiplier
            text = text[: -len(unit)]
            break
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    numeric = float(match.group(0)) * multiplier
    return numeric / 100.0 if percent else numeric


def normalize(df: pd.DataFrame, code: str) -> pd.DataFrame:
    missing = {"报告期"} - set(df.columns)
    if missing:
        raise RuntimeError(f"{code}: missing required columns {sorted(missing)}")
    out = pd.DataFrame()
    for source, target in COLUMN_MAP.items():
        if source not in df.columns:
            out[target] = pd.NA
            continue
        if target == "report_date":
            out[target] = pd.to_datetime(df[source], errors="coerce")
        else:
            out[target] = [
                _parse_value(value, percent=target in PERCENT_COLUMNS) for value in df[source]
            ]
    out = out.dropna(subset=["report_date"]).drop_duplicates("report_date", keep="last")
    out = out.sort_values("report_date").set_index("report_date")
    if len(out) < MIN_ROWS:
        raise RuntimeError(f"{code}: insufficient normalized rows {len(out)}")
    out["code"] = code
    out["source"] = "ths_financial_abstract"
    out["fetched_at"] = datetime.now().isoformat()
    return out


def fetch_one(code: str) -> pd.DataFrame:
    ak = _import_akshare()
    raw = ak.stock_financial_abstract_ths(symbol=code, indicator="按报告期")
    df = pd.DataFrame(raw)
    if df.empty:
        raise RuntimeError(f"{code}: empty THS financial abstract response")
    return normalize(df, code)


def write_atomically(code: str, df: pd.DataFrame) -> None:
    out_path = FUNDAMENTALS_DIR / f"{code}.parquet"
    tmp_path = out_path.with_name(f"{out_path.name}.{os.getpid()}.tmp")
    try:
        df.to_parquet(tmp_path)
        tmp_path.replace(out_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _worker(code: str, result_queue: QueueType[dict[str, Any]]) -> None:
    try:
        out_path = FUNDAMENTALS_DIR / f"{code}.parquet"
        if SKIP_EXISTING and out_path.exists():
            result_queue.put({"status": "ok", "code": code, "skipped": True})
            return
        df = fetch_one(code)
        write_atomically(code, df)
        result_queue.put({"status": "ok", "code": code, "rows": len(df), "skipped": False})
    except Exception as exc:
        result_queue.put({"status": "fail", "code": code, "error": str(exc)})


def _start(code: str) -> tuple[Process, QueueType[dict[str, Any]], float]:
    result_queue: QueueType[dict[str, Any]] = Queue(maxsize=1)
    process = Process(target=_worker, args=(code, result_queue))
    process.start()
    return process, result_queue, time.time()


def main() -> None:
    codes = load_codes()
    total = len(codes)
    next_idx = 0
    completed = 0
    ok = 0
    skipped = 0
    failures: list[dict[str, str]] = []
    active: dict[str, tuple[Process, QueueType[dict[str, Any]], float]] = {}
    started = time.time()
    print(
        f"Fetching THS fundamentals: total={total} workers={MAX_WORKERS} "
        f"timeout={SYMBOL_TIMEOUT_SECONDS:.1f}s skip_existing={SKIP_EXISTING}",
        flush=True,
    )
    while next_idx < total or active:
        while next_idx < total and len(active) < MAX_WORKERS:
            code = codes[next_idx]
            active[code] = _start(code)
            next_idx += 1
        progressed = False
        now = time.time()
        for code, (process, result_queue, process_started) in list(active.items()):
            if process.is_alive() and now - process_started <= SYMBOL_TIMEOUT_SECONDS:
                continue
            if process.is_alive():
                process.terminate()
                process.join(5)
                result: dict[str, Any] = {
                    "status": "fail",
                    "code": code,
                    "error": f"timed out after {SYMBOL_TIMEOUT_SECONDS:.1f}s",
                }
            elif result_queue.empty():
                process.join(1)
                result = {"status": "fail", "code": code, "error": "worker returned no result"}
            else:
                process.join(1)
                result = result_queue.get()
            active.pop(code)
            completed += 1
            progressed = True
            if result["status"] == "ok":
                ok += 1
                if result.get("skipped"):
                    skipped += 1
            else:
                failures.append(
                    {"code": str(result.get("code", code)), "error": str(result.get("error", ""))}
                )
            if completed <= 5 or completed % 100 == 0:
                elapsed = time.time() - started
                rate = completed / max(elapsed, 0.1) * 60.0
                eta = (total - completed) / max(rate, 0.01)
                print(
                    f"[{completed}/{total}] ok={ok} skipped={skipped} fail={len(failures)} "
                    f"| {rate:.1f}/min | ETA {eta:.0f}min",
                    flush=True,
                )
        if not progressed:
            time.sleep(0.1)
    summary = {
        "timestamp": datetime.now().isoformat(),
        "source": "ths_financial_abstract",
        "total": total,
        "success": ok,
        "skipped": skipped,
        "failed": len(failures),
        "failures": failures,
        "elapsed_s": round(time.time() - started, 1),
    }
    (FUNDAMENTALS_DIR / "fetch_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Done: ok={ok} skipped={skipped} failed={len(failures)}")
    if failures:
        raise RuntimeError(f"fundamentals refresh had {len(failures)} failed symbols")


if __name__ == "__main__":
    main()
