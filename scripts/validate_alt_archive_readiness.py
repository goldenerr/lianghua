#!/usr/bin/env python3
"""Validate archive-backed alternative-data feature evidence."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from _paths import ALT_FEATURES_DIR


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--summary-file",
        default=os.getenv("QUANT_ALT_FEATURE_SUMMARY_FILE", "v20_archive_alt_feature_summary.json"),
        help="Feature summary JSON path or filename under data/alt_features.",
    )
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        default=os.getenv("QUANT_ALT_ARCHIVE_ALLOW_INCOMPLETE", "0") == "1",
        help="Allow live fallbacks. This is research-only and must not open production gates.",
    )
    parser.add_argument(
        "--min-stock-rows",
        type=int,
        default=int(os.getenv("QUANT_ALT_ARCHIVE_MIN_STOCK_ROWS", "1")),
        help="Minimum required stock feature rows.",
    )
    parser.add_argument(
        "--min-crisis-rows",
        type=int,
        default=int(os.getenv("QUANT_ALT_ARCHIVE_MIN_CRISIS_ROWS", "1")),
        help="Minimum required crisis-asset feature rows.",
    )
    parser.add_argument(
        "--archive-date",
        default=os.getenv("QUANT_ALT_ARCHIVE_DATE", ""),
        help="Optional expected archive date in YYYYMMDD format.",
    )
    return parser.parse_args()


def _summary_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or path.parent != Path("."):
        return path
    return ALT_FEATURES_DIR / path


def _require(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def _validate(summary: dict[str, Any], args: argparse.Namespace) -> list[str]:
    failures: list[str] = []
    _require(bool(summary.get("archive_mode")), "archive_mode must be true", failures)
    _require(
        bool(summary.get("archive_manifest_sha256")),
        "archive_manifest_sha256 is required",
        failures,
    )
    if args.archive_date:
        _require(
            str(summary.get("archive_date")) == str(args.archive_date),
            f"archive_date must be {args.archive_date}",
            failures,
        )
    _require(
        int(summary.get("stock_feature_rows", 0)) >= args.min_stock_rows,
        f"stock_feature_rows must be >= {args.min_stock_rows}",
        failures,
    )
    _require(
        int(summary.get("crisis_asset_rows", 0)) >= args.min_crisis_rows,
        f"crisis_asset_rows must be >= {args.min_crisis_rows}",
        failures,
    )
    fallbacks = list(summary.get("live_fallback_files") or [])
    complete = bool(summary.get("archive_complete_for_builder"))
    if not args.allow_incomplete:
        _require(complete, "archive_complete_for_builder must be true", failures)
        _require(not fallbacks, f"live_fallback_files must be empty: {fallbacks}", failures)
    return failures


def main() -> None:
    args = _parse_args()
    path = _summary_path(str(args.summary_file))
    if not path.exists():
        raise FileNotFoundError(f"summary file does not exist: {path}")
    summary = json.loads(path.read_text(encoding="utf-8"))
    failures = _validate(summary, args)
    if failures:
        raise RuntimeError("; ".join(failures))
    print(
        "Alternative-data archive readiness OK: "
        f"summary={path} archive_date={summary.get('archive_date')} "
        f"manifest_sha256={summary.get('archive_manifest_sha256')} "
        f"complete={summary.get('archive_complete_for_builder')}",
        flush=True,
    )


if __name__ == "__main__":
    main()
