#!/usr/bin/env python3
"""Fetch 25-year adjusted A-share daily bars from AkShare/Sina.

Environment overrides:
  QUANT_FETCH_START=20000101
  QUANT_FETCH_END=20260529
  QUANT_FETCH_TARGET_POLICY=local  # local|today; ignored when QUANT_FETCH_END is set
  QUANT_FETCH_MODE=incremental  # incremental|stale|full
  QUANT_FETCH_FORCE_FULL=0
  QUANT_FETCH_SLEEP_SECONDS=2

Each symbol is written through a temporary parquet file and atomically replaced
only after validation succeeds, so a failed refresh does not corrupt old data.
By default the script only refreshes missing/stale symbols; set
QUANT_FETCH_FORCE_FULL=1 for an explicit full replacement run.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from multiprocessing import Process, Queue
from multiprocessing.queues import Queue as QueueType
from pathlib import Path
from typing import Any, cast

import pandas as pd
from _paths import DATA_DIR, STOCK_LIST

DATA_DIR.mkdir(parents=True, exist_ok=True)

TARGET_POLICY = os.getenv("QUANT_FETCH_TARGET_POLICY", "local").strip().lower()
START_DATE = os.getenv("QUANT_FETCH_START", "20000101")
FETCH_MODE = os.getenv("QUANT_FETCH_MODE", "incremental").strip().lower()
FORCE_FULL = os.getenv("QUANT_FETCH_FORCE_FULL", "0").strip().lower() in {"1", "true", "yes"}
SLEEP_SECONDS = float(os.getenv("QUANT_FETCH_SLEEP_SECONDS", "2"))
MIN_ROWS = int(os.getenv("QUANT_FETCH_MIN_ROWS", "50"))
ADJUST = os.getenv("QUANT_FETCH_ADJUST", "qfq")
SYMBOL_TIMEOUT_SECONDS = float(os.getenv("QUANT_FETCH_SYMBOL_TIMEOUT_SECONDS", "20"))
MAX_WORKERS = max(1, int(os.getenv("QUANT_FETCH_MAX_WORKERS", "1")))
REQUIRED_COLUMNS = ["open", "high", "low", "close", "volume", "amount", "turnover"]
VALID_FETCH_MODES = {"incremental", "stale", "full"}
VALID_TARGET_POLICIES = {"local", "today"}


def _latest_local_end_date() -> str | None:
    max_date: pd.Timestamp | None = None
    for file_path in DATA_DIR.glob("*.parquet"):
        try:
            df = pd.read_parquet(file_path)
        except Exception:
            continue
        if df.empty:
            continue
        idx_max = pd.Timestamp(df.index.max()).normalize()
        max_date = idx_max if max_date is None or idx_max > max_date else max_date
    return max_date.strftime("%Y%m%d") if max_date is not None else None


def _resolve_end_date() -> str:
    explicit = os.getenv("QUANT_FETCH_END")
    if explicit:
        return explicit
    if TARGET_POLICY not in VALID_TARGET_POLICIES:
        raise RuntimeError(
            f"invalid QUANT_FETCH_TARGET_POLICY={TARGET_POLICY!r}; "
            f"expected {VALID_TARGET_POLICIES}"
        )
    if TARGET_POLICY == "today":
        return datetime.now().strftime("%Y%m%d")
    return _latest_local_end_date() or datetime.now().strftime("%Y%m%d")


END_DATE = _resolve_end_date()


def _import_akshare() -> Any:
    try:
        import akshare as ak
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "akshare is required to refresh market data. "
            "Install the project data-sources extra before running this script."
        ) from exc
    return ak


def load_codes() -> list[str]:
    raw = json.loads(STOCK_LIST.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise RuntimeError(f"stock list must be a JSON string array: {STOCK_LIST}")
    return cast(list[str], raw)


def _end_timestamp() -> pd.Timestamp:
    return pd.Timestamp(datetime.strptime(END_DATE, "%Y%m%d")).normalize()


def _symbol_path(code: str) -> Path:
    return DATA_DIR / f"{code}.parquet"


def symbol_is_current(code: str, end_ts: pd.Timestamp) -> bool:
    """Return True when the local parquet already reaches the requested end date."""

    path = _symbol_path(code)
    if not path.exists():
        return False
    try:
        df = pd.read_parquet(path)
    except Exception:
        return False
    if df.empty:
        return False
    try:
        max_date = pd.Timestamp(df.index.max()).normalize()
    except Exception:
        return False
    return max_date >= end_ts


def determine_codes_to_fetch(codes: list[str]) -> tuple[list[str], list[str], str]:
    if FETCH_MODE not in VALID_FETCH_MODES:
        raise RuntimeError(f"invalid QUANT_FETCH_MODE={FETCH_MODE!r}; expected {VALID_FETCH_MODES}")
    effective_mode = "full" if FORCE_FULL else FETCH_MODE
    if effective_mode == "full":
        return codes, [], effective_mode
    end_ts = _end_timestamp()
    to_fetch = [code for code in codes if not symbol_is_current(code, end_ts)]
    to_fetch_set = set(to_fetch)
    skipped = [code for code in codes if code not in to_fetch_set]
    return to_fetch, skipped, effective_mode


def to_sina(code: str) -> str:
    """Convert 600519.SH to sh600519 and 000001.SZ to sz000001."""

    compact = (
        code.replace(".SH", "").replace(".SZ", "").replace(".sh", "").replace(".sz", "").strip()
    )
    return f"sh{compact}" if compact.startswith("6") else f"sz{compact}"


def fetch_one(ak: Any, code: str) -> pd.DataFrame:
    raw_df = ak.stock_zh_a_daily(
        symbol=to_sina(code),
        start_date=START_DATE,
        end_date=END_DATE,
        adjust=ADJUST,
    )
    df = pd.DataFrame(raw_df)
    if df is None or df.empty or len(df) < MIN_ROWS:
        raise RuntimeError(f"{code}: insufficient rows from data provider")
    missing = [column for column in REQUIRED_COLUMNS if column not in df.columns]
    if missing:
        raise RuntimeError(f"{code}: missing columns {missing}")
    df["date"] = pd.to_datetime(df["date"], errors="raise")
    df = df.set_index("date").sort_index()
    return df[REQUIRED_COLUMNS].astype(float)


def write_symbol_atomically(code: str, df: pd.DataFrame) -> None:
    out_path = DATA_DIR / f"{code}.parquet"
    tmp_path = out_path.with_name(f"{out_path.name}.{os.getpid()}.tmp")
    try:
        df.to_parquet(tmp_path)
        tmp_path.replace(out_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def summarize_dataset() -> dict[str, Any]:
    total_rows = 0
    min_date: pd.Timestamp | None = None
    max_date: pd.Timestamp | None = None
    readable = 0
    for file_path in DATA_DIR.glob("*.parquet"):
        try:
            df = pd.read_parquet(file_path)
        except Exception:
            continue
        if df.empty:
            continue
        readable += 1
        total_rows += len(df)
        idx_min = pd.Timestamp(df.index.min())
        idx_max = pd.Timestamp(df.index.max())
        min_date = idx_min if min_date is None or idx_min < min_date else min_date
        max_date = idx_max if max_date is None or idx_max > max_date else max_date
    return {
        "readable_files": readable,
        "total_rows": total_rows,
        "start": str(min_date.date()) if min_date is not None else "",
        "end": str(max_date.date()) if max_date is not None else "",
    }


def _fetch_worker(code: str, result_queue: QueueType[dict[str, str]]) -> None:
    try:
        ak = _import_akshare()
        df = fetch_one(ak, code)
        write_symbol_atomically(code, df)
        result_queue.put({"code": code, "status": "ok"})
    except Exception as exc:
        result_queue.put({"code": code, "status": "fail", "error": str(exc)})


def fetch_one_with_timeout(code: str) -> dict[str, str]:
    result_queue: QueueType[dict[str, str]] = Queue(maxsize=1)
    process = Process(target=_fetch_worker, args=(code, result_queue))
    process.start()
    process.join(SYMBOL_TIMEOUT_SECONDS)
    if process.is_alive():
        process.terminate()
        process.join(5)
        return {
            "code": code,
            "status": "fail",
            "error": f"symbol fetch timed out after {SYMBOL_TIMEOUT_SECONDS:.1f}s",
        }
    if process.exitcode not in (0, None) and result_queue.empty():
        return {"code": code, "status": "fail", "error": f"worker exited {process.exitcode}"}
    if result_queue.empty():
        return {"code": code, "status": "fail", "error": "worker returned no result"}
    return result_queue.get()


def _start_symbol_process(code: str) -> tuple[Process, QueueType[dict[str, str]], float]:
    result_queue: QueueType[dict[str, str]] = Queue(maxsize=1)
    process = Process(target=_fetch_worker, args=(code, result_queue))
    process.start()
    return process, result_queue, time.time()


def _collect_finished_result(
    code: str,
    process: Process,
    result_queue: QueueType[dict[str, str]],
) -> dict[str, str]:
    process.join(1)
    if process.exitcode not in (0, None) and result_queue.empty():
        return {"code": code, "status": "fail", "error": f"worker exited {process.exitcode}"}
    if result_queue.empty():
        return {"code": code, "status": "fail", "error": "worker returned no result"}
    return result_queue.get()


def main() -> None:
    codes = load_codes()
    requested_total = len(codes)
    codes_to_fetch, skipped, effective_mode = determine_codes_to_fetch(codes)
    total = len(codes_to_fetch)
    print(
        f"股票池: {requested_total} 只 | 待刷新: {total} 只 | 已跳过: {len(skipped)} 只 "
        f"| mode={effective_mode} | 数据源: akshare/sina | {START_DATE}->{END_DATE} "
        f"| symbol_timeout={SYMBOL_TIMEOUT_SECONDS:.1f}s | workers={MAX_WORKERS}",
        flush=True,
    )
    if total == 0:
        dataset = summarize_dataset()
        summary = {
            "timestamp": datetime.now().isoformat(),
            "source": "akshare/sina",
            "adjust": ADJUST,
            "mode": effective_mode,
            "target_policy": TARGET_POLICY,
            "start": START_DATE,
            "end": END_DATE,
            "actual_range": f"{dataset['start']} -> {dataset['end']}",
            "total_requested": requested_total,
            "total_to_fetch": 0,
            "skipped_current": len(skipped),
            "success": 0,
            "failed": 0,
            "failures": [],
            "readable_files": dataset["readable_files"],
            "total_rows": dataset["total_rows"],
            "elapsed_s": 0.0,
        }
        (DATA_DIR / "fetch_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("本地数据已覆盖目标日期，无需发起行情请求。", flush=True)
        print(f"数据范围: {summary['actual_range']} | 总行数: {dataset['total_rows']:,}")
        return

    ok = 0
    failed: list[dict[str, str]] = []
    started = time.time()
    completed = 0
    next_idx = 0
    active: dict[str, tuple[Process, QueueType[dict[str, str]], float]] = {}

    while next_idx < total or active:
        while next_idx < total and len(active) < MAX_WORKERS:
            code = codes_to_fetch[next_idx]
            active[code] = _start_symbol_process(code)
            next_idx += 1

        progressed = False
        now = time.time()
        for code, (process, result_queue, process_started) in list(active.items()):
            result: dict[str, str] | None = None
            if process.is_alive():
                if now - process_started <= SYMBOL_TIMEOUT_SECONDS:
                    continue
                process.terminate()
                process.join(5)
                result = {
                    "code": code,
                    "status": "fail",
                    "error": f"symbol fetch timed out after {SYMBOL_TIMEOUT_SECONDS:.1f}s",
                }
            else:
                result = _collect_finished_result(code, process, result_queue)

            active.pop(code)
            completed += 1
            progressed = True
            if result["status"] == "ok":
                ok += 1
            else:
                failed.append({"code": code, "error": result.get("error", "unknown error")})

            if completed <= 5 or completed % 100 == 0:
                elapsed = time.time() - started
                rate_per_min = completed / max(elapsed, 0.1) * 60.0
                eta_min = (total - completed) / max(rate_per_min, 0.01)
                print(
                    f"[{completed}/{total}] ok={ok} fail={len(failed)} "
                    f"| {rate_per_min:.1f}/min | ETA {eta_min:.0f}min",
                    flush=True,
                )

        if not progressed:
            time.sleep(0.1)
            continue

        if SLEEP_SECONDS > 0:
            time.sleep(SLEEP_SECONDS)

    elapsed = time.time() - started
    dataset = summarize_dataset()
    summary = {
        "timestamp": datetime.now().isoformat(),
        "source": "akshare/sina",
        "adjust": ADJUST,
        "mode": effective_mode,
        "target_policy": TARGET_POLICY,
        "start": START_DATE,
        "end": END_DATE,
        "actual_range": f"{dataset['start']} -> {dataset['end']}",
        "total_requested": requested_total,
        "total_to_fetch": total,
        "skipped_current": len(skipped),
        "success": ok,
        "failed": len(failed),
        "failures": failed,
        "readable_files": dataset["readable_files"],
        "total_rows": dataset["total_rows"],
        "elapsed_s": round(elapsed, 1),
    }
    (DATA_DIR / "fetch_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n完成: {ok}/{total} 成功, {len(failed)} 失败, 用时 {elapsed / 60:.1f}min")
    print(f"数据范围: {summary['actual_range']} | 总行数: {dataset['total_rows']:,}")
    if failed:
        raise RuntimeError(f"market data refresh had {len(failed)} failed symbols")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--one":
        queue: QueueType[dict[str, str]] = Queue(maxsize=1)
        _fetch_worker(sys.argv[2], queue)
        result = queue.get() if not queue.empty() else {"status": "fail", "error": "no result"}
        if result["status"] != "ok":
            raise RuntimeError(result.get("error", "unknown error"))
        raise SystemExit(0)
    main()
