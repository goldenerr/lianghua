#!/usr/bin/env python3
"""Emit the V26 external production-evidence gate report.

This script intentionally does not fabricate evidence. It only evaluates refs
supplied through a JSON file or environment JSON and writes a fail-closed report.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _paths import RESULTS_DIR

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_trading.deployment import evaluate_production_readiness

DEFAULT_OUTPUT_JSON = "quant_external_evidence_gate_v26.json"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-file",
        default=os.getenv("QUANT_PRODUCTION_EVIDENCE_FILE", ""),
        help="Optional JSON file containing production evidence refs.",
    )
    parser.add_argument(
        "--output-json",
        default=os.getenv("QUANT_V26_EXTERNAL_EVIDENCE_OUTPUT_JSON", DEFAULT_OUTPUT_JSON),
        help="Output JSON path or filename under data/backtest_results.",
    )
    parser.add_argument(
        "--require-production-ready",
        action="store_true",
        help="Exit non-zero unless all external evidence refs are present and trusted.",
    )
    return parser.parse_args()


def _resolve(path_value: str, default_dir: Path) -> Path:
    path = Path(path_value)
    if path.is_absolute() or path.parent != Path("."):
        return path
    return default_dir / path


def _load_evidence(evidence_file: str) -> dict[str, str]:
    raw_env = os.getenv("QUANT_PRODUCTION_EVIDENCE_JSON", "").strip()
    if raw_env:
        loaded = json.loads(raw_env)
    elif evidence_file:
        loaded = json.loads(Path(evidence_file).read_text(encoding="utf-8"))
    else:
        loaded = {}
    if not isinstance(loaded, dict):
        raise RuntimeError("production evidence must be a JSON object")
    return {str(key): str(value) for key, value in loaded.items()}


def main() -> None:
    args = _parse_args()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output_json_path = _resolve(str(args.output_json), RESULTS_DIR)
    evidence = _load_evidence(str(args.evidence_file))
    report = evaluate_production_readiness(evidence)
    payload: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V26-external-evidence-gate",
        "production_ready": report.passed,
        "provided_keys": sorted(evidence),
        "required_keys": list(report.evidence_keys),
        "missing_or_untrusted_blockers": list(report.blockers),
        "evidence": {key: evidence.get(key, "") for key in report.evidence_keys},
        "note": (
            "Local files, placeholders, mock refs and public-source probes are not accepted "
            "as production evidence. Supply external WORM/provider/approval/broker/paper refs."
        ),
    }
    output_json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        "V26 external evidence gate: "
        f"production_ready={report.passed} blockers={len(report.blockers)}",
        flush=True,
    )
    print(f"Wrote {output_json_path}", flush=True)
    if args.require_production_ready and not report.passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
