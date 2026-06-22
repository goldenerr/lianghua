#!/usr/bin/env python3
"""Fetch northbound, main-fund, block-trade, and margin-flow signals.

These sources are heterogeneous: some are true histories, while others are
current provider snapshots. The summary file records that distinction so factor
research can enforce point-in-time usage.
"""

from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from multiprocessing import Process, Queue
from multiprocessing.queues import Queue as QueueType
from pathlib import Path
from typing import Any, cast

import pandas as pd
from _paths import FLOW_SIGNALS_DIR, PROJECT_DIR, STOCK_LIST

FLOW_SIGNALS_DIR.mkdir(parents=True, exist_ok=True)

MARGIN_START = os.getenv("QUANT_FLOW_MARGIN_START", "20260529")
MARGIN_END = os.getenv("QUANT_FLOW_MARGIN_END", MARGIN_START)
MAX_SYMBOLS = int(os.getenv("QUANT_FLOW_MAX_SYMBOLS", "20"))
SYMBOL_OFFSET = int(os.getenv("QUANT_FLOW_SYMBOL_OFFSET", "0"))
MAX_WORKERS = max(1, int(os.getenv("QUANT_FLOW_MAX_WORKERS", "1")))
MAX_MARGIN_WORKERS = max(
    1,
    int(os.getenv("QUANT_FLOW_MARGIN_MAX_WORKERS", os.getenv("QUANT_FLOW_MAX_WORKERS", "1"))),
)
MARGIN_TASK_TIMEOUT_SECONDS = float(os.getenv("QUANT_FLOW_MARGIN_TASK_TIMEOUT_SECONDS", "45"))
SLEEP_SECONDS = float(os.getenv("QUANT_FLOW_SLEEP_SECONDS", "0.2"))
RETRIES = int(os.getenv("QUANT_FLOW_RETRIES", "3"))
SKIP_EXISTING = os.getenv("QUANT_FLOW_SKIP_EXISTING", "1") == "1"
COMBINE_ALL_EXISTING = os.getenv("QUANT_FLOW_COMBINE_ALL_EXISTING", "1") == "1"
FETCH_NORTHBOUND_AGGREGATE = os.getenv("QUANT_FLOW_FETCH_NORTHBOUND_AGGREGATE", "1") == "1"
FETCH_MAIN_FUND = os.getenv("QUANT_FLOW_FETCH_MAIN_FUND", "1") == "1"
FETCH_BIG_DEAL = os.getenv("QUANT_FLOW_FETCH_BIG_DEAL", "1") == "1"
FETCH_NORTHBOUND_HOLDINGS = os.getenv("QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS", "1") == "1"
FETCH_FUND_FLOW_RANKS = os.getenv("QUANT_FLOW_FETCH_FUND_FLOW_RANKS", "1") == "1"
FETCH_MARGIN_DETAILS = os.getenv("QUANT_FLOW_FETCH_MARGIN_DETAILS", "1") == "1"
ALLOW_PARTIAL = os.getenv("QUANT_FLOW_ALLOW_PARTIAL", "1") == "1"
DEFAULT_MARGIN_CALENDAR = PROJECT_DIR / "data" / "benchmarks" / "idx_000300.parquet"

FUND_FLOW_HORIZONS = tuple(
    item.strip() for item in os.getenv("QUANT_FLOW_RANK_HORIZONS", "3日排行,5日排行").split(",") if item
)

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
        raise RuntimeError("akshare is required to fetch flow signal data") from exc
    return ak


def _load_codes() -> list[str]:
    raw = json.loads(STOCK_LIST.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise RuntimeError(f"stock list must be a JSON string array: {STOCK_LIST}")
    return [str(item).zfill(6) for item in cast(list[str], raw)]


def _selected_codes() -> list[str]:
    codes = _load_codes()
    if SYMBOL_OFFSET < 0:
        raise ValueError("QUANT_FLOW_SYMBOL_OFFSET must be >= 0")
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


def _load_existing_frames(directory: Path) -> list[pd.DataFrame]:
    frames: list[pd.DataFrame] = []
    for path in sorted(directory.glob("*.parquet")):
        df = pd.read_parquet(path)
        if not df.empty:
            frames.append(df)
    return frames


def _load_existing_margin_frames() -> list[pd.DataFrame]:
    frames: list[pd.DataFrame] = []
    for pattern in ("margin_sse_*.parquet", "margin_szse_*.parquet"):
        for path in sorted(FLOW_SIGNALS_DIR.glob(pattern)):
            df = pd.read_parquet(path)
            if "date" not in df.columns and "asof_date" in df.columns:
                df["date"] = pd.to_datetime(df["asof_date"], errors="coerce")
            elif "date" in df.columns and "asof_date" in df.columns:
                df["date"] = pd.to_datetime(df["date"], errors="coerce").fillna(
                    pd.to_datetime(df["asof_date"], errors="coerce")
                )
            if not df.empty:
                frames.append(df)
    return frames


def _find_column(columns: list[str], *patterns: str) -> str | None:
    for pattern in patterns:
        for column in columns:
            if pattern in str(column):
                return str(column)
    return None


def _standardize_code_column(df: pd.DataFrame) -> pd.DataFrame:
    columns = [str(column) for column in df.columns]
    code_col = _find_column(columns, "股票代码", "证券代码", "代码")
    if code_col and code_col in df.columns:
        df["code"] = df[code_col].astype(str).str.extract(r"(\d{6})", expand=False).str.zfill(6)
    return df


def _standardize_date_column(df: pd.DataFrame) -> pd.DataFrame:
    columns = [str(column) for column in df.columns]
    date_col = _find_column(columns, "日期", "交易日", "信用交易日期", "成交时间")
    if date_col and date_col in df.columns:
        target = "datetime" if "时间" in date_col else "date"
        df[target] = pd.to_datetime(df[date_col], errors="coerce")
    return df


def _numericize_common_columns(df: pd.DataFrame) -> pd.DataFrame:
    for column in list(df.columns):
        text = str(column)
        if any(key in text for key in ("额", "量", "余额", "价格", "涨跌幅", "换手率", "净买额")):
            parsed = [_parse_number(value, percent="率" in text or "幅" in text) for value in df[column]]
            if any(value is not None for value in parsed):
                df[f"{text}_numeric"] = parsed
    return df


def _normalize_snapshot(raw: pd.DataFrame, source: str, asof_date: pd.Timestamp | None = None) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()
    df = raw.copy()
    df.columns = [str(column) for column in df.columns]
    df = _standardize_code_column(df)
    df = _standardize_date_column(df)
    df = _numericize_common_columns(df)
    df["asof_date"] = asof_date or pd.Timestamp(datetime.now().date())
    if "date" not in df.columns:
        df["date"] = pd.to_datetime(df["asof_date"], errors="coerce")
    else:
        df["date"] = pd.to_datetime(df["date"], errors="coerce").fillna(
            pd.to_datetime(df["asof_date"], errors="coerce")
        )
    df["source"] = source
    df["fetched_at"] = datetime.now().isoformat()
    return df


def _date_range(start: str, end: str) -> list[str]:
    calendar_path = Path(os.getenv("QUANT_FLOW_MARGIN_CALENDAR_FILE", str(DEFAULT_MARGIN_CALENDAR)))
    if calendar_path.exists():
        frame = pd.read_parquet(calendar_path)
        if isinstance(frame.index, pd.DatetimeIndex):
            dates = pd.to_datetime(frame.index, errors="coerce")
        elif "date" in frame.columns:
            dates = pd.to_datetime(frame["date"], errors="coerce")
        else:
            dates = pd.DatetimeIndex([])
        selected = [
            date.strftime("%Y%m%d")
            for date in dates.dropna().normalize()
            if pd.Timestamp(start) <= date <= pd.Timestamp(end)
        ]
        if selected:
            return sorted(set(selected))
    dates = pd.bdate_range(pd.Timestamp(start), pd.Timestamp(end))
    return [date.strftime("%Y%m%d") for date in dates]


def fetch_northbound_aggregate(ak: Any) -> dict[str, Any]:
    if not FETCH_NORTHBOUND_AGGREGATE:
        return {"status": "disabled"}
    path = FLOW_SIGNALS_DIR / "northbound_aggregate.parquet"
    if SKIP_EXISTING and path.exists():
        df = pd.read_parquet(path)
        return {"status": "skipped", "rows": len(df), "path": str(path)}
    raw = _fetch_with_retry(ak.stock_hsgt_hist_em, symbol="北向资金")
    df = _normalize_snapshot(raw, "eastmoney_hsgt_hist_northbound")
    _write_atomically(path, df)
    return {"status": "ok", "rows": len(df), "path": str(path)}


def _fetch_one_northbound_holding(code: str) -> tuple[dict[str, Any] | None, pd.DataFrame | None, dict[str, str] | None]:
    ak = _import_akshare()
    out_dir = FLOW_SIGNALS_DIR / "northbound_holdings"
    path = out_dir / f"{code}.parquet"
    try:
        if SKIP_EXISTING and path.exists():
            df = pd.read_parquet(path)
            status = "skipped"
        else:
            raw = _fetch_with_retry(ak.stock_hsgt_individual_em, symbol=code)
            df = _normalize_snapshot(raw, "eastmoney_hsgt_individual")
            if "code" not in df.columns:
                df.insert(0, "code", code)
            _write_atomically(path, df)
            status = "ok"
            time.sleep(SLEEP_SECONDS)
        row = {"code": code, "status": status, "rows": len(df)}
        return row, df if not df.empty else None, None
    except Exception as exc:
        return None, None, {"code": code, "error": str(exc)}


def fetch_northbound_holdings(ak: Any) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if not FETCH_NORTHBOUND_HOLDINGS:
        return [{"status": "disabled"}], []
    out_dir = FLOW_SIGNALS_DIR / "northbound_holdings"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    frames: list[pd.DataFrame] = []
    codes = _selected_codes()
    if MAX_WORKERS == 1 or len(codes) <= 1:
        for code in codes:
            row, df, failure = _fetch_one_northbound_holding(code)
            if row is not None:
                rows.append(row)
            if df is not None:
                frames.append(df)
            if failure is not None:
                failures.append(failure)
    else:
        order = {code: idx for idx, code in enumerate(codes)}
        completed = 0
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_one_northbound_holding, code): code for code in codes}
            for future in as_completed(futures):
                row, df, failure = future.result()
                completed += 1
                if row is not None:
                    rows.append(row)
                if df is not None:
                    frames.append(df)
                if failure is not None:
                    failures.append(failure)
                if completed <= 5 or completed % 25 == 0:
                    print(
                        f"[northbound {completed}/{len(codes)}] ok={len(rows)} "
                        f"fail={len(failures)} workers={MAX_WORKERS}",
                        flush=True,
                    )
        rows.sort(key=lambda item: order.get(str(item.get("code", "")), len(order)))
        failures.sort(key=lambda item: order.get(str(item.get("code", "")), len(order)))
    if COMBINE_ALL_EXISTING:
        frames = _load_existing_frames(out_dir)
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    _write_atomically(FLOW_SIGNALS_DIR / "northbound_holdings.parquet", combined)
    return rows, failures


def fetch_fund_flow_ranks(ak: Any) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if not FETCH_FUND_FLOW_RANKS:
        return [{"status": "disabled"}], []
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    frames: list[pd.DataFrame] = []
    asof_date = pd.Timestamp(datetime.now().date())
    for horizon in FUND_FLOW_HORIZONS:
        safe = re.sub(r"\W+", "_", horizon)
        path = FLOW_SIGNALS_DIR / f"fund_flow_rank_{safe}.parquet"
        try:
            if SKIP_EXISTING and path.exists():
                df = pd.read_parquet(path)
                status = "skipped"
            else:
                raw = _fetch_with_retry(ak.stock_fund_flow_individual, symbol=horizon)
                df = _normalize_snapshot(raw, f"eastmoney_fund_flow_rank_{horizon}", asof_date)
                df["horizon"] = horizon
                _write_atomically(path, df)
                status = "ok"
                time.sleep(SLEEP_SECONDS)
            if not df.empty:
                frames.append(df)
            rows.append({"horizon": horizon, "status": status, "rows": len(df)})
        except Exception as exc:
            failures.append({"horizon": horizon, "error": str(exc)})
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    _write_atomically(FLOW_SIGNALS_DIR / "fund_flow_ranks.parquet", combined)
    return rows, failures


def fetch_main_fund(ak: Any) -> dict[str, Any]:
    if not FETCH_MAIN_FUND:
        return {"status": "disabled"}
    path = FLOW_SIGNALS_DIR / "main_fund_flow_hs.parquet"
    if SKIP_EXISTING and path.exists():
        df = pd.read_parquet(path)
        return {"status": "skipped", "rows": len(df), "path": str(path)}
    raw = _fetch_with_retry(ak.stock_main_fund_flow, symbol="沪深A股")
    df = _normalize_snapshot(raw, "eastmoney_main_fund_flow_hs")
    _write_atomically(path, df)
    return {"status": "ok", "rows": len(df), "path": str(path)}


def fetch_big_deal(ak: Any) -> dict[str, Any]:
    if not FETCH_BIG_DEAL:
        return {"status": "disabled"}
    path = FLOW_SIGNALS_DIR / "big_deal_current.parquet"
    if SKIP_EXISTING and path.exists():
        df = pd.read_parquet(path)
        return {"status": "skipped", "rows": len(df), "path": str(path)}
    raw = _fetch_with_retry(ak.stock_fund_flow_big_deal)
    df = _normalize_snapshot(raw, "eastmoney_big_deal_current")
    _write_atomically(path, df)
    return {"status": "ok", "rows": len(df), "path": str(path)}


def _margin_fetcher(ak: Any, exchange: str) -> Any:
    if exchange == "sse":
        return ak.stock_margin_detail_sse
    if exchange == "szse":
        return ak.stock_margin_detail_szse
    raise ValueError(f"unsupported margin exchange: {exchange}")


def _fetch_one_margin_detail(date: str, exchange: str) -> dict[str, Any]:
    ak = _import_akshare()
    path = FLOW_SIGNALS_DIR / f"margin_{exchange}_{date}.parquet"
    if SKIP_EXISTING and path.exists():
        df = pd.read_parquet(path)
        return {"date": date, "exchange": exchange, "status": "skipped", "rows": len(df)}
    raw = _fetch_with_retry(_margin_fetcher(ak, exchange), date=date)
    df = _normalize_snapshot(raw, f"{exchange}_margin_detail", pd.Timestamp(date))
    df["exchange"] = exchange
    _write_atomically(path, df)
    time.sleep(SLEEP_SECONDS)
    return {"date": date, "exchange": exchange, "status": "ok", "rows": len(df)}


def _margin_worker(date: str, exchange: str, result_queue: QueueType[dict[str, Any]]) -> None:
    try:
        result_queue.put({"status": "ok", "result": _fetch_one_margin_detail(date, exchange)})
    except Exception as exc:
        result_queue.put(
            {
                "status": "fail",
                "date": date,
                "exchange": exchange,
                "error": str(exc),
            }
        )


def _start_margin_task(
    date: str,
    exchange: str,
) -> tuple[Process, QueueType[dict[str, Any]], float]:
    result_queue: QueueType[dict[str, Any]] = Queue(maxsize=1)
    process = Process(target=_margin_worker, args=(date, exchange, result_queue))
    process.start()
    return process, result_queue, time.time()


def _fetch_margin_details_parallel(
    tasks: list[tuple[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    active: dict[tuple[str, str], tuple[Process, QueueType[dict[str, Any]], float]] = {}
    next_idx = 0
    completed = 0
    total = len(tasks)
    while next_idx < total or active:
        while next_idx < total and len(active) < MAX_MARGIN_WORKERS:
            date, exchange = tasks[next_idx]
            active[(date, exchange)] = _start_margin_task(date, exchange)
            next_idx += 1
        progressed = False
        now = time.time()
        for task, (process, result_queue, started) in list(active.items()):
            date, exchange = task
            if process.is_alive() and now - started <= MARGIN_TASK_TIMEOUT_SECONDS:
                continue
            if process.is_alive():
                process.terminate()
                process.join(5)
                result: dict[str, Any] = {
                    "status": "fail",
                    "date": date,
                    "exchange": exchange,
                    "error": f"timed out after {MARGIN_TASK_TIMEOUT_SECONDS:.1f}s",
                }
            elif result_queue.empty():
                process.join(1)
                result = {
                    "status": "fail",
                    "date": date,
                    "exchange": exchange,
                    "error": "worker returned no result",
                }
            else:
                process.join(1)
                result = result_queue.get()
            active.pop(task)
            completed += 1
            progressed = True
            if result.get("status") == "ok":
                rows.append(result["result"])
            else:
                failures.append(
                    {
                        "date": str(result.get("date", date)),
                        "exchange": str(result.get("exchange", exchange)),
                        "error": str(result.get("error", "")),
                    }
                )
            if completed <= 5 or completed % 50 == 0:
                print(
                    f"[margin {completed}/{total}] ok={len(rows)} fail={len(failures)} "
                    f"workers={MAX_MARGIN_WORKERS}",
                    flush=True,
                )
        if not progressed:
            time.sleep(0.1)
    return rows, failures


def fetch_margin_details(ak: Any) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if not FETCH_MARGIN_DETAILS:
        return [{"status": "disabled"}], []
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    frames: list[pd.DataFrame] = []
    tasks = [(date, exchange) for date in _date_range(MARGIN_START, MARGIN_END) for exchange in ("sse", "szse")]
    if MAX_MARGIN_WORKERS == 1 or len(tasks) <= 1:
        for date, exchange in tasks:
            try:
                row = _fetch_one_margin_detail(date, exchange)
                path = FLOW_SIGNALS_DIR / f"margin_{exchange}_{date}.parquet"
                df = pd.read_parquet(path) if path.exists() else pd.DataFrame()
                if not df.empty:
                    frames.append(df)
                rows.append(row)
            except Exception as exc:
                failures.append({"date": date, "exchange": exchange, "error": str(exc)})
    else:
        rows, failures = _fetch_margin_details_parallel(tasks)
    if COMBINE_ALL_EXISTING or MAX_MARGIN_WORKERS > 1:
        frames = _load_existing_margin_frames()
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    _write_atomically(FLOW_SIGNALS_DIR / "margin_details.parquet", combined)
    return rows, failures


def main() -> None:
    started = time.time()
    ak = _import_akshare()
    failures: list[dict[str, str]] = []
    print(
        f"Fetching flow signals: symbol_offset={SYMBOL_OFFSET} max_symbols={MAX_SYMBOLS} "
        f"workers={MAX_WORKERS} combine_all_existing={COMBINE_ALL_EXISTING} "
        f"margin={MARGIN_START}->{MARGIN_END} margin_workers={MAX_MARGIN_WORKERS} "
        f"allow_partial={ALLOW_PARTIAL}",
        flush=True,
    )
    sections: dict[str, Any] = {}
    for section_name, fetcher in (
        ("northbound_aggregate", lambda: fetch_northbound_aggregate(ak)),
        ("main_fund_flow", lambda: fetch_main_fund(ak)),
        ("big_deal", lambda: fetch_big_deal(ak)),
    ):
        try:
            sections[section_name] = fetcher()
        except Exception as exc:
            sections[section_name] = {"status": "failed", "error": str(exc)}
            failures.append({"section": section_name, "error": str(exc)})
    holding_rows, holding_failures = fetch_northbound_holdings(ak)
    rank_rows, rank_failures = fetch_fund_flow_ranks(ak)
    margin_rows, margin_failures = fetch_margin_details(ak)
    failures.extend(holding_failures)
    failures.extend(rank_failures)
    failures.extend(margin_failures)
    summary = {
        "timestamp": datetime.now().isoformat(),
        "source": [
            "eastmoney_hsgt_hist_akshare",
            "eastmoney_hsgt_individual_akshare",
            "eastmoney_fund_flow_akshare",
            "sse_szse_margin_detail_akshare",
        ],
        "point_in_time_warning": (
            "fund flow rank, main fund flow and big deal endpoints are provider snapshots; "
            "persist daily archives before using them as historical PIT factors."
        ),
        "sections": sections,
        "northbound_holdings": holding_rows,
        "fund_flow_ranks": rank_rows,
        "margin_details": margin_rows,
        "symbol_offset": SYMBOL_OFFSET,
        "max_symbols": MAX_SYMBOLS,
        "max_workers": MAX_WORKERS,
        "max_margin_workers": MAX_MARGIN_WORKERS,
        "margin_task_timeout_seconds": MARGIN_TASK_TIMEOUT_SECONDS,
        "combine_all_existing": COMBINE_ALL_EXISTING,
        "failed": len(failures),
        "failures": failures,
        "elapsed_s": round(time.time() - started, 1),
    }
    (FLOW_SIGNALS_DIR / "fetch_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"Done flow signals: failures={len(failures)}", flush=True)
    if failures and not ALLOW_PARTIAL:
        raise RuntimeError(f"flow signal fetch had {len(failures)} failures")


if __name__ == "__main__":
    main()
