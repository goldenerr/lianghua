#!/usr/bin/env python3
"""Run resumable V26 PIT alpha-panel backfills.

This driver intentionally remains a research/data-ingestion helper. It does
not downgrade production evidence blockers and it never treats provider-missing
symbols as covered. Its job is to remove manual batch babysitting: plan, fetch,
single-worker retry, record repeated public-provider gaps, then re-plan.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _paths import FLOW_SIGNALS_DIR, INTRADAY_DIR, PROJECT_DIR, RESULTS_DIR

PLAN_JSON = RESULTS_DIR / "quant_pit_backfill_plan_v26_non_largecap_2000.json"
PLAN_CSV = RESULTS_DIR / "quant_pit_backfill_plan_v26_non_largecap_2000.csv"
READINESS_JSON = RESULTS_DIR / "quant_pit_alpha_panel_readiness_v26_non_largecap_2000.json"
READINESS_CSV = RESULTS_DIR / "quant_pit_alpha_panel_readiness_v26_non_largecap_2000.csv"
DEFAULT_STOCK_LIST = PROJECT_DIR / "data" / "stock_list_non_largecap_2000_v26.json"
NORTHBOUND_UNAVAILABLE = FLOW_SIGNALS_DIR / "northbound_unavailable_symbols_v26.json"
FLOW_SUMMARY = FLOW_SIGNALS_DIR / "fetch_summary.json"
INTRADAY_SUMMARY = INTRADAY_DIR / "fetch_summary.json"
RUN_SUMMARY = RESULTS_DIR / "quant_v26_accelerated_backfill_run.json"


@dataclass(frozen=True)
class BatchResult:
    panel: str
    offset: int
    max_symbols: int
    returncode: int
    failed_codes: list[str]
    failures: list[dict[str, str]]
    elapsed_s: float


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stock-list",
        default=os.getenv("QUANT_V26_STOCK_LIST", str(DEFAULT_STOCK_LIST)),
        help="V26 target universe JSON. Defaults to the 2,000-name non-largecap universe.",
    )
    parser.add_argument(
        "--min-target-symbols",
        type=int,
        default=int(os.getenv("QUANT_V26_MIN_TARGET_SYMBOLS", "2000")),
        help="Refuse to run if the target universe contains fewer symbols.",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=int(os.getenv("QUANT_V26_BACKFILL_MAX_CYCLES", "0")),
        help="Maximum plan/fetch cycles. 0 means continue until no local batches remain.",
    )
    parser.add_argument(
        "--northbound-workers",
        type=int,
        default=int(os.getenv("QUANT_V26_NORTHBOUND_WORKERS", "4")),
        help="Parallel public-provider workers for northbound holdings.",
    )
    parser.add_argument(
        "--intraday-workers",
        type=int,
        default=int(os.getenv("QUANT_V26_INTRADAY_WORKERS", "4")),
        help="Process-pool workers for intraday microstructure fetches.",
    )
    parser.add_argument(
        "--batch-timeout-seconds",
        type=float,
        default=float(os.getenv("QUANT_V26_BACKFILL_BATCH_TIMEOUT_SECONDS", "900")),
        help="Timeout per batch command.",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=float(os.getenv("QUANT_V26_BACKFILL_SLEEP_SECONDS", "0.1")),
        help="Provider sleep interval passed to fetchers.",
    )
    parser.add_argument(
        "--stop-on-retryable-failure",
        action=argparse.BooleanOptionalAction,
        default=os.getenv("QUANT_V26_STOP_ON_RETRYABLE_FAILURE", "1") == "1",
        help="Stop when retry failures do not look like deterministic provider gaps.",
    )
    parser.add_argument(
        "--refresh-readiness-each-cycle",
        action=argparse.BooleanOptionalAction,
        default=os.getenv("QUANT_V26_REFRESH_READINESS_EACH_CYCLE", "0") == "1",
        help="Run the heavier readiness/evidence validators after every cycle.",
    )
    return parser.parse_args()


def _assert_target_universe(path: Path, min_symbols: int) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise RuntimeError(f"target stock list must be a JSON string array: {path}")
    if len(raw) < min_symbols:
        raise RuntimeError(
            f"refusing to run on {len(raw)} symbols from {path}; "
            f"minimum required is {min_symbols}"
        )


def _run_python(script: str, *, env: dict[str, str], timeout: float) -> int:
    merged = os.environ.copy()
    merged.update(env)
    command = [sys.executable, script]
    print(f"\n$ {' '.join(command)}", flush=True)
    started = time.time()
    completed = subprocess.run(
        command,
        cwd=PROJECT_DIR,
        env=merged,
        timeout=timeout,
        check=False,
    )
    elapsed = time.time() - started
    print(f"[command done] rc={completed.returncode} elapsed_s={elapsed:.1f}", flush=True)
    return int(completed.returncode)


def _refresh_plan(*, stock_list: Path, timeout: float) -> dict[str, Any]:
    rc = _run_python(
        "scripts/plan_pit_backfill_v25.py",
        env={
            "QUANT_STOCK_LIST": str(stock_list),
            "QUANT_V25_OUTPUT_JSON": PLAN_JSON.name,
            "QUANT_V25_OUTPUT_CSV": PLAN_CSV.name,
        },
        timeout=timeout,
    )
    if rc != 0:
        raise RuntimeError("failed to refresh V26 PIT backfill plan")
    return json.loads(PLAN_JSON.read_text(encoding="utf-8"))


def _refresh_readiness(*, stock_list: Path, timeout: float) -> None:
    rc = _run_python(
        "scripts/validate_pit_alpha_panels_v24.py",
        env={
            "QUANT_STOCK_LIST": str(stock_list),
            "QUANT_V24_OUTPUT_JSON": READINESS_JSON.name,
            "QUANT_V24_OUTPUT_CSV": READINESS_CSV.name,
        },
        timeout=timeout,
    )
    if rc != 0:
        raise RuntimeError("failed to refresh V26 PIT alpha-panel readiness")
    rc = _run_python("scripts/validate_external_evidence_v26.py", env={}, timeout=timeout)
    if rc != 0:
        raise RuntimeError("failed to refresh V26 external evidence gate")


def _failure_codes(summary_path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not summary_path.exists():
        return [], []
    raw = json.loads(summary_path.read_text(encoding="utf-8"))
    failures: list[dict[str, str]] = []
    for item in raw.get("failures", []):
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        if not isinstance(code, str):
            continue
        failures.append(
            {
                "code": code.zfill(6),
                "error": str(item.get("error", "")),
            }
        )
    return sorted({item["code"] for item in failures}), failures


def _provider_gap_error(error: str) -> bool:
    text = error.lower()
    transient_markers = (
        "timeout",
        "timed out",
        "connection",
        "ssl",
        "reset",
        "proxy",
        "network",
        "temporary",
        "429",
        "403",
        "502",
        "503",
        "504",
    )
    if any(marker in text for marker in transient_markers):
        return False
    return "nonetype" in text or "none type" in text or "'none'" in text


def _load_unavailable() -> dict[str, Any]:
    if not NORTHBOUND_UNAVAILABLE.exists():
        return {
            "version": "V26-northbound-provider-unavailable",
            "research_only": True,
            "symbols": [],
            "evidence": [],
            "production_blocker": (
                "Requires entitled full-universe northbound holdings provider or alternate source; "
                "do not synthesize holdings."
            ),
        }
    return json.loads(NORTHBOUND_UNAVAILABLE.read_text(encoding="utf-8"))


def _record_northbound_unavailable(
    *,
    offset: int,
    first_failures: list[dict[str, str]],
    retry_failures: list[dict[str, str]],
) -> list[str]:
    first_by_code = {item["code"]: item for item in first_failures}
    retry_by_code = {item["code"]: item for item in retry_failures}
    confirmed = [
        code
        for code, item in retry_by_code.items()
        if code in first_by_code and _provider_gap_error(item.get("error", ""))
    ]
    if not confirmed:
        return []

    data = _load_unavailable()
    symbols = {str(item).zfill(6) for item in data.get("symbols", [])}
    evidence = list(data.get("evidence", []))
    evidence_by_symbol = {
        str(item.get("symbol", "")).zfill(6): item
        for item in evidence
        if isinstance(item, dict) and item.get("symbol")
    }
    for code in confirmed:
        symbols.add(code)
        error = retry_by_code[code].get("error", "")
        existing = evidence_by_symbol.get(code)
        if existing is None:
            evidence.append(
                {
                    "symbol": code,
                    "panel": "northbound_stock_holding_history",
                    "provider": "akshare.stock_hsgt_individual_em",
                    "observed_offsets": [offset],
                    "observed_retries": ["workers=4", "workers=1"],
                    "error": error,
                    "disposition": "exclude_from_fetch_batches_only; not counted as covered",
                }
            )
        else:
            observed_offsets = existing.setdefault("observed_offsets", [])
            if offset not in observed_offsets:
                observed_offsets.append(offset)
            observed_retries = existing.setdefault("observed_retries", [])
            for retry in ("workers=4", "workers=1"):
                if retry not in observed_retries:
                    observed_retries.append(retry)
            existing["error"] = error
            existing["disposition"] = "exclude_from_fetch_batches_only; not counted as covered"

    data["symbols"] = sorted(symbols)
    data["evidence"] = sorted(evidence, key=lambda item: str(item.get("symbol", "")))
    tmp_path = NORTHBOUND_UNAVAILABLE.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(NORTHBOUND_UNAVAILABLE)
    return confirmed


def _run_northbound_batch(
    *,
    stock_list: Path,
    offset: int,
    max_symbols: int,
    workers: int,
    sleep_seconds: float,
    timeout: float,
) -> BatchResult:
    started = time.time()
    rc = _run_python(
        "scripts/fetch_flow_signals.py",
        env={
            "QUANT_STOCK_LIST": str(stock_list),
            "QUANT_FLOW_SKIP_EXISTING": "1",
            "QUANT_FLOW_COMBINE_ALL_EXISTING": "1",
            "QUANT_FLOW_MAX_WORKERS": str(max(1, workers)),
            "QUANT_FLOW_SLEEP_SECONDS": str(sleep_seconds),
            "QUANT_FLOW_FETCH_NORTHBOUND_AGGREGATE": "0",
            "QUANT_FLOW_FETCH_NORTHBOUND_HOLDINGS": "1",
            "QUANT_FLOW_FETCH_MAIN_FUND": "0",
            "QUANT_FLOW_FETCH_BIG_DEAL": "0",
            "QUANT_FLOW_FETCH_FUND_FLOW_RANKS": "0",
            "QUANT_FLOW_FETCH_MARGIN_DETAILS": "0",
            "QUANT_FLOW_SYMBOL_OFFSET": str(offset),
            "QUANT_FLOW_MAX_SYMBOLS": str(max_symbols),
        },
        timeout=timeout,
    )
    failed_codes, failures = _failure_codes(FLOW_SUMMARY)
    return BatchResult(
        panel="northbound",
        offset=offset,
        max_symbols=max_symbols,
        returncode=rc,
        failed_codes=failed_codes,
        failures=failures,
        elapsed_s=time.time() - started,
    )


def _run_intraday_batch(
    *,
    stock_list: Path,
    offset: int,
    max_symbols: int,
    workers: int,
    sleep_seconds: float,
    timeout: float,
) -> BatchResult:
    started = time.time()
    rc = _run_python(
        "scripts/fetch_intraday_microstructure.py",
        env={
            "QUANT_STOCK_LIST": str(stock_list),
            "QUANT_INTRADAY_SKIP_EXISTING": "1",
            "QUANT_INTRADAY_COMBINE_ALL_EXISTING": "1",
            "QUANT_INTRADAY_MAX_WORKERS": str(max(1, workers)),
            "QUANT_INTRADAY_SLEEP_SECONDS": str(sleep_seconds),
            "QUANT_INTRADAY_SYMBOL_OFFSET": str(offset),
            "QUANT_INTRADAY_MAX_SYMBOLS": str(max_symbols),
        },
        timeout=timeout,
    )
    failed_codes, failures = _failure_codes(INTRADAY_SUMMARY)
    return BatchResult(
        panel="intraday",
        offset=offset,
        max_symbols=max_symbols,
        returncode=rc,
        failed_codes=failed_codes,
        failures=failures,
        elapsed_s=time.time() - started,
    )


def _first_batch(plan: dict[str, Any], panel: str) -> dict[str, Any] | None:
    batches = plan.get("batches", {}).get(panel, [])
    if not isinstance(batches, list) or not batches:
        return None
    batch = batches[0]
    if not isinstance(batch, dict):
        return None
    return batch


def _coverage(plan: dict[str, Any]) -> dict[str, Any]:
    coverage = plan.get("coverage", {})
    return coverage if isinstance(coverage, dict) else {}


def _write_run_summary(events: list[dict[str, Any]], plan: dict[str, Any]) -> None:
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V26-accelerated-backfill-run",
        "research_only": True,
        "coverage": _coverage(plan),
        "batch_counts": plan.get("batch_counts", {}),
        "events": events,
        "production_blockers": [
            "provider-unavailable symbols are evidence only and are not counted as covered",
            "historical intraday depth still needs daily archive accumulation or an entitled minute feed",
            "broker-backed borrow availability and external evidence refs remain release blockers",
        ],
    }
    RUN_SUMMARY.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    stock_list = Path(args.stock_list).resolve()
    _assert_target_universe(stock_list, args.min_target_symbols)
    print(
        f"[target] stock_list={stock_list} min_symbols={args.min_target_symbols}",
        flush=True,
    )
    events: list[dict[str, Any]] = []
    plan = _refresh_plan(stock_list=stock_list, timeout=args.batch_timeout_seconds)
    cycles = 0
    while True:
        coverage = _coverage(plan)
        print(
            "\n[plan] "
            f"northbound={coverage.get('northbound_existing_target_symbols')}/2000 "
            f"northbound_fetchable_missing={coverage.get('northbound_fetchable_missing_target_symbols')} "
            f"intraday={coverage.get('intraday_existing_target_symbols')}/2000 "
            f"intraday_missing={coverage.get('intraday_missing_target_symbols')}",
            flush=True,
        )
        northbound_batch = _first_batch(plan, "northbound")
        intraday_batch = _first_batch(plan, "intraday")
        if northbound_batch is None and intraday_batch is None:
            print("[done] no remaining local northbound/intraday batches", flush=True)
            break
        if args.max_cycles and cycles >= args.max_cycles:
            print(f"[stop] reached max_cycles={args.max_cycles}", flush=True)
            break
        cycles += 1
        cycle_event: dict[str, Any] = {"cycle": cycles, "started_at": datetime.now(timezone.utc).isoformat()}

        if northbound_batch is not None:
            offset = int(northbound_batch["offset"])
            max_symbols = int(northbound_batch["max_symbols"])
            print(
                f"[cycle {cycles}] northbound offset={offset} symbols={max_symbols}",
                flush=True,
            )
            first = _run_northbound_batch(
                stock_list=stock_list,
                offset=offset,
                max_symbols=max_symbols,
                workers=args.northbound_workers,
                sleep_seconds=args.sleep_seconds,
                timeout=args.batch_timeout_seconds,
            )
            cycle_event["northbound_first"] = first.__dict__
            if first.failed_codes:
                print(
                    f"[cycle {cycles}] retrying northbound failures with one worker: "
                    f"{first.failed_codes}",
                    flush=True,
                )
                retry = _run_northbound_batch(
                    stock_list=stock_list,
                    offset=offset,
                    max_symbols=max_symbols,
                    workers=1,
                    sleep_seconds=max(args.sleep_seconds, 0.2),
                    timeout=args.batch_timeout_seconds,
                )
                confirmed = _record_northbound_unavailable(
                    offset=offset,
                    first_failures=first.failures,
                    retry_failures=retry.failures,
                )
                retryable = [item for item in retry.failures if item["code"] not in confirmed]
                cycle_event["northbound_retry"] = retry.__dict__
                cycle_event["northbound_provider_unavailable_added"] = confirmed
                if retryable and args.stop_on_retryable_failure:
                    cycle_event["stopped_on_retryable_failure"] = retryable
                    events.append(cycle_event)
                    plan = _refresh_plan(stock_list=stock_list, timeout=args.batch_timeout_seconds)
                    _write_run_summary(events, plan)
                    raise RuntimeError(f"northbound retryable failures remain: {retryable}")

        plan = _refresh_plan(stock_list=stock_list, timeout=args.batch_timeout_seconds)
        intraday_batch = _first_batch(plan, "intraday")
        if intraday_batch is not None:
            offset = int(intraday_batch["offset"])
            max_symbols = int(intraday_batch["max_symbols"])
            print(f"[cycle {cycles}] intraday offset={offset} symbols={max_symbols}", flush=True)
            first = _run_intraday_batch(
                stock_list=stock_list,
                offset=offset,
                max_symbols=max_symbols,
                workers=args.intraday_workers,
                sleep_seconds=args.sleep_seconds,
                timeout=args.batch_timeout_seconds,
            )
            cycle_event["intraday_first"] = first.__dict__
            if first.failed_codes:
                print(
                    f"[cycle {cycles}] retrying intraday failures with one worker: {first.failed_codes}",
                    flush=True,
                )
                retry = _run_intraday_batch(
                    stock_list=stock_list,
                    offset=offset,
                    max_symbols=max_symbols,
                    workers=1,
                    sleep_seconds=max(args.sleep_seconds, 0.2),
                    timeout=args.batch_timeout_seconds,
                )
                cycle_event["intraday_retry"] = retry.__dict__
                if retry.failed_codes and args.stop_on_retryable_failure:
                    cycle_event["stopped_on_intraday_failure"] = retry.failures
                    events.append(cycle_event)
                    plan = _refresh_plan(stock_list=stock_list, timeout=args.batch_timeout_seconds)
                    _write_run_summary(events, plan)
                    raise RuntimeError(f"intraday retry failures remain: {retry.failures}")

        events.append(cycle_event)
        plan = _refresh_plan(stock_list=stock_list, timeout=args.batch_timeout_seconds)
        if args.refresh_readiness_each_cycle:
            _refresh_readiness(stock_list=stock_list, timeout=args.batch_timeout_seconds)
        _write_run_summary(events, plan)

    _refresh_readiness(stock_list=stock_list, timeout=args.batch_timeout_seconds)
    _write_run_summary(events, plan)
    print(f"[summary] wrote {RUN_SUMMARY}", flush=True)


if __name__ == "__main__":
    main()
