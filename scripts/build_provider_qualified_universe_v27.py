#!/usr/bin/env python3
"""Build a provider-qualified non-large-cap V27 universe.

V26 proved that a fixed 2,000-name non-large-cap universe still leaves public
Northbound and margin/short coverage gaps. This builder is stricter: it selects
only names with verified local panel presence. It is still research evidence,
not a production entitlement artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from math import ceil
from pathlib import Path
from typing import Any

import pandas as pd
from _paths import (
    ANALYST_EXPECTATIONS_DIR,
    FLOW_SIGNALS_DIR,
    INTRADAY_DIR,
    PROJECT_DIR,
    RESULTS_DIR,
)

CODE_RE = re.compile(r"(\d{6})")
ALLOWED_STOCK_PREFIXES = (
    "000",
    "001",
    "002",
    "003",
    "300",
    "301",
    "600",
    "601",
    "603",
    "605",
    "688",
)
EXPLICIT_LARGE_CAP_BLACKLIST = {
    "000001",
    "000002",
    "000063",
    "000100",
    "000333",
    "000651",
    "000858",
    "002594",
    "300750",
    "600000",
    "600009",
    "600030",
    "600036",
    "600276",
    "600519",
    "600887",
    "601012",
    "601166",
    "601318",
    "601398",
    "601899",
    "603259",
    "688111",
    "688981",
}

DEFAULT_OUTPUT_LIST = "stock_list_provider_qualified_non_largecap_2000_v27.json"
DEFAULT_OUTPUT_JSON = "quant_provider_qualified_universe_v27.json"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target-size",
        type=int,
        default=int(os.getenv("QUANT_V27_UNIVERSE_TARGET_SIZE", "2000")),
        help="Required provider-qualified non-large-cap universe size.",
    )
    parser.add_argument(
        "--csi300-components",
        default=os.getenv("QUANT_V27_CSI300_COMPONENTS", "data/csi300_components_v26.json"),
        help="CSI300 component evidence JSON path.",
    )
    parser.add_argument(
        "--require-intraday",
        action=argparse.BooleanOptionalAction,
        default=os.getenv("QUANT_V27_REQUIRE_INTRADAY", "1") == "1",
        help="Require local intraday feature coverage in the selected universe.",
    )
    parser.add_argument(
        "--min-analyst-overlap-ratio",
        type=float,
        default=float(os.getenv("QUANT_V27_MIN_ANALYST_OVERLAP_RATIO", "0.95")),
        help="Minimum selected-universe overlap with analyst revision history.",
    )
    parser.add_argument(
        "--output-list",
        default=os.getenv("QUANT_V27_UNIVERSE_OUTPUT_LIST", DEFAULT_OUTPUT_LIST),
        help="Output universe JSON path or filename under data/.",
    )
    parser.add_argument(
        "--output-json",
        default=os.getenv("QUANT_V27_UNIVERSE_OUTPUT_JSON", DEFAULT_OUTPUT_JSON),
        help="Output evidence JSON path or filename under data/backtest_results/.",
    )
    return parser.parse_args()


def _resolve(path_value: str, default_dir: Path) -> Path:
    path = Path(path_value)
    if path.is_absolute() or path.parent != Path("."):
        return path
    return default_dir / path


def _normalize_codes(values: Any) -> list[str]:
    codes: list[str] = []
    for value in values:
        match = CODE_RE.search(str(value))
        if not match:
            continue
        code = match.group(1).zfill(6)
        if code.startswith(ALLOWED_STOCK_PREFIXES) and not code.startswith(("4", "8", "9")):
            codes.append(code)
    return codes


def _load_json_codes(path: Path) -> set[str]:
    if not path.exists():
        return set()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return set(_normalize_codes(raw))
    if isinstance(raw, dict):
        for key in ("codes", "symbols", "stock_codes"):
            values = raw.get(key)
            if isinstance(values, list):
                return set(_normalize_codes(values))
    return set()


def _load_parquet_code_counts(path: Path, column: str = "code") -> Counter[str]:
    if not path.exists():
        return Counter()
    frame = pd.read_parquet(path, columns=[column])
    if column not in frame.columns:
        return Counter()
    return Counter(_normalize_codes(frame[column].dropna().tolist()))


def _sha256_json_list(codes: list[str]) -> str:
    payload = json.dumps(codes, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    if args.target_size <= 0:
        raise ValueError("target-size must be positive")

    data_dir = PROJECT_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    csi300_path = _resolve(str(args.csi300_components), PROJECT_DIR)
    csi300_codes = _load_json_codes(csi300_path)
    if not csi300_codes:
        raise RuntimeError(f"missing CSI300 component evidence: {csi300_path}")
    excluded_large_caps = csi300_codes.union(EXPLICIT_LARGE_CAP_BLACKLIST)

    northbound_counts = _load_parquet_code_counts(FLOW_SIGNALS_DIR / "northbound_holdings.parquet")
    margin_counts = _load_parquet_code_counts(FLOW_SIGNALS_DIR / "margin_details.parquet")
    analyst_counts = _load_parquet_code_counts(
        ANALYST_EXPECTATIONS_DIR / "analyst_ratings_cninfo.parquet"
    )
    intraday_counts = _load_parquet_code_counts(INTRADAY_DIR / "microstructure_features.parquet")

    northbound = set(northbound_counts).difference(excluded_large_caps)
    margin = set(margin_counts).difference(excluded_large_caps)
    analyst = set(analyst_counts).difference(excluded_large_caps)
    intraday = set(intraday_counts).difference(excluded_large_caps)
    analyst_required_count = ceil(args.target_size * args.min_analyst_overlap_ratio)
    if not 0.0 <= args.min_analyst_overlap_ratio <= 1.0:
        raise ValueError("min-analyst-overlap-ratio must be between 0 and 1")

    base_eligible = northbound.intersection(margin)
    primary_eligible = base_eligible.intersection(analyst)
    fallback_eligible = base_eligible.difference(analyst)
    if args.require_intraday:
        primary_eligible = primary_eligible.intersection(intraday)
        fallback_eligible = fallback_eligible.intersection(intraday)

    primary_ranked = sorted(
        primary_eligible,
        key=lambda code: (
            -analyst_counts[code],
            -margin_counts[code],
            -northbound_counts[code],
            -intraday_counts[code],
            code,
        ),
    )
    fallback_ranked = sorted(
        fallback_eligible,
        key=lambda code: (
            -margin_counts[code],
            -northbound_counts[code],
            -intraday_counts[code],
            code,
        ),
    )
    selected = (primary_ranked + fallback_ranked)[: args.target_size]
    selected_analyst_overlap = len(set(selected).intersection(analyst))
    output_list_path = _resolve(str(args.output_list), data_dir)
    output_json_path = _resolve(str(args.output_json), RESULTS_DIR)

    report = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V27-provider-qualified-non-largecap-universe",
        "research_only": True,
        "production_ready": False,
        "target_size": args.target_size,
        "selected_count": len(selected),
        "require_intraday": args.require_intraday,
        "min_analyst_overlap_ratio": args.min_analyst_overlap_ratio,
        "min_analyst_overlap_count": analyst_required_count,
        "selected_analyst_overlap_count": selected_analyst_overlap,
        "selected_analyst_overlap_ratio": (
            round(selected_analyst_overlap / len(selected), 6) if selected else 0.0
        ),
        "passes_target_size": len(selected) >= args.target_size,
        "passes_analyst_overlap": selected_analyst_overlap >= analyst_required_count,
        "universe_path": str(output_list_path),
        "universe_sha256": _sha256_json_list(selected),
        "first_codes": selected[:20],
        "last_codes": selected[-20:],
        "large_cap_exclusion": {
            "method": "CSI300 components plus explicit large-cap blacklist",
            "csi300_evidence_path": str(csi300_path),
            "csi300_component_count": len(csi300_codes),
            "explicit_large_cap_blacklist_count": len(EXPLICIT_LARGE_CAP_BLACKLIST),
            "selected_csi300_overlap_count": len(set(selected).intersection(csi300_codes)),
        },
        "panel_entity_counts_non_largecap": {
            "northbound": len(northbound),
            "margin_short": len(margin),
            "analyst_revision": len(analyst),
            "intraday": len(intraday),
            "northbound_margin": len(northbound.intersection(margin)),
            "northbound_margin_analyst": len(northbound.intersection(margin).intersection(analyst)),
            "northbound_margin_intraday": len(
                northbound.intersection(margin).intersection(intraday)
            ),
            "northbound_margin_analyst_intraday": len(
                northbound.intersection(margin).intersection(analyst).intersection(intraday)
            ),
        },
        "selection_rules": [
            "exclude CSI300 and explicit large-cap blacklist",
            "require local northbound holdings panel coverage",
            "require local margin/short detail panel coverage",
            "prefer local analyst revision panel coverage",
            "allow limited non-analyst fallback only while selected analyst overlap stays above threshold",
            "require local intraday microstructure coverage when require_intraday=true",
            "rank analyst-covered names by analyst, margin, northbound and intraday record counts",
            "rank fallback names by margin, northbound and intraday record counts",
        ],
        "production_blockers": [
            "provider-qualified local universe is research evidence only",
            "production still requires approved security master, provider entitlement and broker borrow evidence",
        ],
    }
    _write_report(output_json_path, report)
    if len(selected) < args.target_size or selected_analyst_overlap < analyst_required_count:
        raise RuntimeError(
            f"selected={len(selected)} analyst_overlap={selected_analyst_overlap}; "
            f"need selected>={args.target_size} and analyst_overlap>={analyst_required_count}. "
            f"Wrote diagnostic report to {output_json_path}"
        )

    output_list_path.write_text(
        json.dumps(selected, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "V27 provider-qualified universe: "
        f"selected={len(selected)} require_intraday={args.require_intraday} "
        f"sha256={report['universe_sha256']}",
        flush=True,
    )
    print(f"Wrote {output_list_path}", flush=True)
    print(f"Wrote {output_json_path}", flush=True)


if __name__ == "__main__":
    main()
