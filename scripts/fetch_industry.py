#!/usr/bin/env python3
"""Fetch real A-share industry classifications.

Default provider is CNInfo's industry-change endpoint. It returns auditable
classification standards per stock, including SW/申万, CNInfo/巨潮, CSI/中证 and
CSRC/证监会. The script writes atomically and refuses to replace the industry
file unless the requested coverage threshold is met.

By default it backfills only symbols missing from the existing industry parquet.
Set QUANT_INDUSTRY_FORCE_FULL=1 to refresh the full stock universe.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from multiprocessing import Process, Queue
from multiprocessing.queues import Queue as QueueType
from pathlib import Path
from typing import Any, cast

import pandas as pd
from _paths import INDUSTRY_PATH, STOCK_LIST

START_DATE = os.getenv("QUANT_INDUSTRY_START", "19900101")
END_DATE = os.getenv("QUANT_INDUSTRY_END", datetime.now().strftime("%Y%m%d"))
MIN_COVERAGE = float(os.getenv("QUANT_INDUSTRY_MIN_COVERAGE", "0.95"))
FORCE_FULL = os.getenv("QUANT_INDUSTRY_FORCE_FULL", "0").strip().lower() in {"1", "true", "yes"}
MAX_WORKERS = max(1, int(os.getenv("QUANT_INDUSTRY_MAX_WORKERS", "4")))
SYMBOL_TIMEOUT_SECONDS = float(os.getenv("QUANT_INDUSTRY_SYMBOL_TIMEOUT_SECONDS", "30"))
PARTIAL_ROWS_PATH = Path(
    os.getenv("QUANT_INDUSTRY_PARTIAL_ROWS", "/private/tmp/lianghua_industry_rows.json")
)
FAILURES_PATH = Path(
    os.getenv("QUANT_INDUSTRY_FAILURES", "/private/tmp/lianghua_industry_failures.json")
)

STANDARD_PRIORITY = (
    "申银万国行业分类标准",
    "巨潮行业分类标准",
    "中证行业分类标准",
    "证监会行业分类标准",
    "中国上市公司协会上市公司行业分类标准",
)
INDUSTRY_COLUMNS = ("行业中类", "行业大类", "行业次类", "行业门类")


def _import_akshare() -> Any:
    try:
        import akshare as ak
    except ModuleNotFoundError as exc:
        raise RuntimeError("akshare is required for CNInfo industry data") from exc
    return ak


def load_codes() -> list[str]:
    raw = json.loads(STOCK_LIST.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise RuntimeError(f"stock list must be a JSON string array: {STOCK_LIST}")
    return cast(list[str], raw)


def load_existing_industry() -> pd.DataFrame:
    if not INDUSTRY_PATH.exists() or FORCE_FULL:
        return pd.DataFrame()
    try:
        df = pd.read_parquet(INDUSTRY_PATH)
    except Exception:
        return pd.DataFrame()
    if df.empty or "code" not in df.columns:
        return pd.DataFrame()
    df = df.copy()
    df["code"] = df["code"].astype(str).str.zfill(6)
    return df.drop_duplicates(subset=["code"], keep="last")


def determine_codes_to_fetch(codes: list[str], existing: pd.DataFrame) -> tuple[list[str], list[str]]:
    if FORCE_FULL or existing.empty:
        return codes, []
    existing_codes = set(existing["code"].astype(str))
    to_fetch = [code for code in codes if code not in existing_codes]
    skipped = [code for code in codes if code in existing_codes]
    return to_fetch, skipped


def _clean(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _select_preferred_row(df: pd.DataFrame) -> dict[str, str]:
    if df.empty:
        raise RuntimeError("empty CNInfo industry response")
    work = df.copy()
    work["变更日期"] = pd.to_datetime(work["变更日期"], errors="coerce")
    for standard in STANDARD_PRIORITY:
        subset = work[work["分类标准"].astype(str).str.contains(standard, na=False)]
        if standard in {"申银万国行业分类标准", "巨潮行业分类标准"}:
            subset = subset[~subset["分类标准"].astype(str).str.contains("旧", na=False)]
        if subset.empty:
            continue
        row = subset.sort_values("变更日期").iloc[-1]
        industry = next(
            (_clean(row.get(col)) for col in INDUSTRY_COLUMNS if _clean(row.get(col))), ""
        )
        if not industry:
            continue
        change_date = pd.Timestamp(row["变更日期"])
        return {
            "code": _clean(row.get("证券代码")),
            "name": _clean(row.get("新证券简称")),
            "industry": industry,
            "industry_mid": _clean(row.get("行业中类")),
            "industry_large": _clean(row.get("行业大类")),
            "industry_sub": _clean(row.get("行业次类")),
            "industry_sector": _clean(row.get("行业门类")),
            "industry_code": _clean(row.get("行业编码")),
            "classification_standard": _clean(row.get("分类标准")),
            "classification_standard_code": _clean(row.get("分类标准编码")),
            "change_date": str(change_date.date()),
            "source": "cninfo",
        }
    raise RuntimeError("no preferred industry classification found")


def fetch_cninfo_industry_one(code: str) -> dict[str, str]:
    ak = _import_akshare()
    df = ak.stock_industry_change_cninfo(
        symbol=code,
        start_date=START_DATE,
        end_date=END_DATE,
    )
    selected = _select_preferred_row(pd.DataFrame(df))
    selected["code"] = code
    return selected


def _worker(code: str, result_queue: QueueType[dict[str, Any]]) -> None:
    try:
        result_queue.put({"status": "ok", "data": fetch_cninfo_industry_one(code)})
    except Exception as exc:
        result_queue.put({"status": "fail", "code": code, "error": str(exc)})


def _start(code: str) -> tuple[Process, QueueType[dict[str, Any]], float]:
    result_queue: QueueType[dict[str, Any]] = Queue(maxsize=1)
    process = Process(target=_worker, args=(code, result_queue))
    process.start()
    return process, result_queue, time.time()


def fetch_all(codes: list[str]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    next_idx = 0
    completed = 0
    rows: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    active: dict[str, tuple[Process, QueueType[dict[str, Any]], float]] = {}
    started = time.time()
    total = len(codes)
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
                rows.append(cast(dict[str, str], result["data"]))
            else:
                failures.append(
                    {"code": str(result.get("code", code)), "error": str(result.get("error", ""))}
                )
            if completed <= 5 or completed % 100 == 0:
                elapsed = time.time() - started
                rate = completed / max(elapsed, 0.1) * 60.0
                eta = (total - completed) / max(rate, 0.01)
                print(
                    f"[{completed}/{total}] ok={len(rows)} fail={len(failures)} "
                    f"| {rate:.1f}/min | ETA {eta:.0f}min",
                    flush=True,
                )
        if not progressed:
            time.sleep(0.1)
    return rows, failures


def write_atomically(df: pd.DataFrame, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(f"{out_path.name}.{os.getpid()}.tmp")
    try:
        df.to_parquet(tmp_path)
        tmp_path.replace(out_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def main() -> None:
    codes = load_codes()
    existing = load_existing_industry()
    codes_to_fetch, skipped = determine_codes_to_fetch(codes, existing)
    print(
        f"industry universe={len(codes)} | to_fetch={len(codes_to_fetch)} "
        f"| skipped_existing={len(skipped)} | force_full={FORCE_FULL}",
        flush=True,
    )
    rows: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    if codes_to_fetch:
        rows, failures = fetch_all(codes_to_fetch)
    PARTIAL_ROWS_PATH.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    FAILURES_PATH.write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")
    fetched_df = pd.DataFrame(rows)
    frames = [frame for frame in (existing, fetched_df) if not frame.empty]
    merged = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame(columns=["code", "industry", "classification_standard"])
    )
    if "code" in merged.columns:
        merged["code"] = merged["code"].astype(str).str.zfill(6)
    df = merged.drop_duplicates(subset=["code"], keep="last").sort_values("code")
    stock_codes = set(codes)
    covered_codes = set(df["code"].astype(str)) & stock_codes if "code" in df.columns else set()
    coverage = len(covered_codes) / max(len(codes), 1)
    print(
        f"industry coverage={coverage:.2%} covered={len(covered_codes)} "
        f"new_ok={len(rows)} fail={len(failures)}",
        flush=True,
    )
    if coverage < MIN_COVERAGE:
        print(f"failures sample: {failures[:20]}")
        raise RuntimeError(f"industry coverage {coverage:.2%} below threshold {MIN_COVERAGE:.2%}")
    df["fetched_at"] = datetime.now().isoformat()
    write_atomically(df, INDUSTRY_PATH)
    print(f"Saved {len(df)} industry rows to {INDUSTRY_PATH}", flush=True)
    print(df[["code", "industry", "classification_standard"]].head(15).to_string(index=False))
    if failures:
        print(f"Non-blocking failures below threshold: {failures[:20]}")


if __name__ == "__main__":
    main()
