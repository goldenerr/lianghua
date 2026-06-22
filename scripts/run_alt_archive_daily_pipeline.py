#!/usr/bin/env python3
"""Run the daily local alternative-data archive and feature evidence pipeline."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from _paths import ALT_FEATURES_DIR
from archive_alt_data_snapshots import DEFAULT_SOURCES


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive-date",
        default=os.getenv("QUANT_ALT_ARCHIVE_DATE", date.today().strftime("%Y%m%d")),
        help="Archive date in YYYYMMDD format.",
    )
    parser.add_argument(
        "--sources",
        default=os.getenv("QUANT_ALT_ARCHIVE_SOURCES", DEFAULT_SOURCES),
        help="Comma-separated source names for scripts/archive_alt_data_snapshots.py.",
    )
    parser.add_argument(
        "--output-prefix",
        default=os.getenv("QUANT_ALT_ARCHIVE_OUTPUT_PREFIX", ""),
        help="Feature output prefix. Defaults to v21_archive_<archive_date>.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        default=os.getenv("QUANT_ALT_ARCHIVE_OVERWRITE", "0") == "1",
        help="Allow audited local repair of an existing archive date.",
    )
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        default=os.getenv("QUANT_ALT_ARCHIVE_ALLOW_INCOMPLETE", "0") == "1",
        help="Allow live fallback inputs. Research-only; production gates remain blocked.",
    )
    return parser.parse_args()


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, env=env)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def main() -> None:
    args = _parse_args()
    started = time.time()
    archive_date = str(args.archive_date)
    datetime.strptime(archive_date, "%Y%m%d")
    output_prefix = str(args.output_prefix or f"v21_archive_{archive_date}")
    if "/" in output_prefix or "\\" in output_prefix:
        raise ValueError("--output-prefix must be a filename prefix, not a path")

    archive_cmd = [
        sys.executable,
        "scripts/archive_alt_data_snapshots.py",
        "--archive-date",
        archive_date,
        "--sources",
        str(args.sources),
    ]
    if args.overwrite:
        archive_cmd.append("--overwrite")
    _run(archive_cmd)
    _run([sys.executable, "scripts/archive_alt_data_snapshots.py", "--archive-date", archive_date, "--verify"])

    build_env = os.environ.copy()
    build_env.update(
        {
            "QUANT_ALT_FEATURES_USE_ARCHIVE": "1",
            "QUANT_ALT_FEATURES_ARCHIVE_DATE": archive_date,
            "QUANT_ALT_FEATURES_OUTPUT_PREFIX": output_prefix,
        }
    )
    _run([sys.executable, "scripts/build_alt_data_features_v17.py"], env=build_env)

    summary_file = f"{output_prefix}_alt_feature_summary.json"
    validate_cmd = [
        sys.executable,
        "scripts/validate_alt_archive_readiness.py",
        "--summary-file",
        summary_file,
        "--archive-date",
        archive_date,
    ]
    if args.allow_incomplete:
        validate_cmd.append("--allow-incomplete")
    _run(validate_cmd)

    summary = _read_json(ALT_FEATURES_DIR / summary_file)
    report = {
        "timestamp": datetime.now().isoformat(),
        "archive_date": archive_date,
        "sources": [item.strip() for item in str(args.sources).split(",") if item.strip()],
        "output_prefix": output_prefix,
        "summary_file": summary_file,
        "archive_manifest_sha256": summary.get("archive_manifest_sha256"),
        "archive_complete_for_builder": summary.get("archive_complete_for_builder"),
        "live_fallback_files": summary.get("live_fallback_files", []),
        "stock_feature_rows": summary.get("stock_feature_rows"),
        "stock_feature_symbols": summary.get("stock_feature_symbols"),
        "crisis_asset_rows": summary.get("crisis_asset_rows"),
        "research_only": True,
        "production_warning": (
            "Local archive evidence is not an approved external WORM store. "
            "Production readiness still requires WORM/provider/approval evidence."
        ),
        "elapsed_s": round(time.time() - started, 1),
    }
    report_path = ALT_FEATURES_DIR / f"{output_prefix}_pipeline_report.json"
    _write_json(report_path, report)
    print(f"Wrote {report_path}", flush=True)


if __name__ == "__main__":
    main()
