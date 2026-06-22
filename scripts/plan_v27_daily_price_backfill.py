#!/usr/bin/env python3
"""Plan V27 daily OHLCV backfill without refetching current symbols."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from _paths import DATA_DIR, PROJECT_DIR, RESULTS_DIR

DEFAULT_UNIVERSE = PROJECT_DIR / "data" / "stock_list_provider_qualified_non_largecap_2000_v27.json"
DEFAULT_MISSING_LIST = PROJECT_DIR / "data" / "stock_list_v27_daily_price_missing.json"
DEFAULT_PLAN_JSON = RESULTS_DIR / "quant_v27_daily_price_backfill_plan.json"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    parser.add_argument("--end-date", default="20260529")
    parser.add_argument("--min-rows", type=int, default=252)
    parser.add_argument("--min-coverage-ratio", type=float, default=0.95)
    parser.add_argument("--missing-list", default=str(DEFAULT_MISSING_LIST))
    parser.add_argument("--output-json", default=str(DEFAULT_PLAN_JSON))
    return parser.parse_args()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_codes(path: Path) -> list[str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise RuntimeError(f"universe must be a JSON string array: {path}")
    return [str(item).zfill(6) for item in raw]


def _symbol_status(code: str, *, end_ts: pd.Timestamp, min_rows: int) -> dict[str, Any]:
    path = DATA_DIR / f"{code}.parquet"
    if not path.exists():
        return {"code": code, "status": "missing_file", "path": str(path)}
    try:
        frame = pd.read_parquet(path, columns=["close", "volume"])
    except Exception as exc:
        return {"code": code, "status": "unreadable", "path": str(path), "error": str(exc)}
    if frame.empty:
        return {"code": code, "status": "empty", "path": str(path)}
    max_date = pd.Timestamp(frame.index.max()).normalize()
    min_date = pd.Timestamp(frame.index.min()).normalize()
    status = "current"
    failures: list[str] = []
    if len(frame) < min_rows:
        status = "insufficient_history"
        failures.append(f"rows<{min_rows}")
    if max_date < end_ts:
        status = "stale" if status == "current" else status
        failures.append(f"end<{end_ts.date().isoformat()}")
    return {
        "code": code,
        "status": status,
        "path": str(path),
        "rows": int(len(frame)),
        "start": min_date.date().isoformat(),
        "end": max_date.date().isoformat(),
        "failures": failures,
    }


def main() -> None:
    args = _parse_args()
    universe_path = Path(args.universe)
    missing_list_path = Path(args.missing_list)
    output_path = Path(args.output_json)
    end_ts = pd.Timestamp(datetime.strptime(args.end_date, "%Y%m%d")).normalize()
    codes = _read_codes(universe_path)
    statuses = [_symbol_status(code, end_ts=end_ts, min_rows=args.min_rows) for code in codes]
    current = [item["code"] for item in statuses if item["status"] == "current"]
    to_fetch = [item["code"] for item in statuses if item["status"] != "current"]
    by_status: dict[str, int] = {}
    for item in statuses:
        by_status[item["status"]] = by_status.get(item["status"], 0) + 1
    required_current = int(len(codes) * args.min_coverage_ratio)
    plan = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V27-daily-price-backfill-plan",
        "research_only": True,
        "universe": {
            "path": str(universe_path),
            "size": len(codes),
            "sha256": _sha256_file(universe_path),
        },
        "target_end_date": end_ts.date().isoformat(),
        "min_rows": args.min_rows,
        "min_coverage_ratio": args.min_coverage_ratio,
        "required_current_symbols": required_current,
        "current_symbols": len(current),
        "to_fetch_symbols": len(to_fetch),
        "coverage_ratio": round(len(current) / max(len(codes), 1), 6),
        "passes_research_coverage": len(current) >= required_current,
        "status_counts": by_status,
        "missing_list_path": str(missing_list_path),
        "to_fetch": to_fetch,
        "sample_failures": [item for item in statuses if item["status"] != "current"][:50],
    }
    missing_list_path.write_text(json.dumps(to_fetch, ensure_ascii=False, indent=2), encoding="utf-8")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {output_path}")
    print(f"Wrote {missing_list_path}")
    print(
        "V27 daily OHLCV: "
        f"current={len(current)}/{len(codes)} "
        f"to_fetch={len(to_fetch)} "
        f"passes={plan['passes_research_coverage']}"
    )


if __name__ == "__main__":
    main()
