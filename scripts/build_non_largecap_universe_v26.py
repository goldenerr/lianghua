#!/usr/bin/env python3
"""Build the V26 non-large-cap target universe.

The output is a research universe for PIT alpha-panel coverage/backfill. It is
not a production entitlement artifact: live trading still needs an approved
security master, market-cap filter, ST/suspension filter and provider contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from _paths import (
    ANALYST_EXPECTATIONS_DIR,
    FLOW_SIGNALS_DIR,
    INDUSTRY_PATH,
    PROJECT_DIR,
    RESULTS_DIR,
    STOCK_LIST,
)

DEFAULT_OUTPUT_LIST = "stock_list_non_largecap_2000_v26.json"
DEFAULT_OUTPUT_JSON = "quant_non_largecap_universe_v26.json"
DEFAULT_TARGET_SIZE = 2000

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
    # Keep this tiny and auditable. The primary large-cap filter is CSI300.
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


@dataclass(frozen=True)
class CandidateSource:
    name: str
    path: Path
    codes: list[str]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target-size",
        type=int,
        default=int(os.getenv("QUANT_V26_UNIVERSE_TARGET_SIZE", str(DEFAULT_TARGET_SIZE))),
        help="Target non-large-cap stock count.",
    )
    parser.add_argument(
        "--csi300-components",
        default=os.getenv("QUANT_V26_CSI300_COMPONENTS", "data/csi300_components_v26.json"),
        help="CSI300 component evidence JSON path.",
    )
    parser.add_argument(
        "--output-list",
        default=os.getenv("QUANT_V26_UNIVERSE_OUTPUT_LIST", DEFAULT_OUTPUT_LIST),
        help="Output universe JSON path or filename under data/.",
    )
    parser.add_argument(
        "--output-json",
        default=os.getenv("QUANT_V26_UNIVERSE_OUTPUT_JSON", DEFAULT_OUTPUT_JSON),
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
        if code.startswith(ALLOWED_STOCK_PREFIXES):
            codes.append(code)
    return codes


def _unique_preserve_order(codes: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for code in codes:
        if code in seen:
            continue
        seen.add(code)
        ordered.append(code)
    return ordered


def _load_json_codes(path: Path) -> list[str]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return _normalize_codes(raw)
    if isinstance(raw, dict):
        for key in ("codes", "symbols", "stock_codes"):
            values = raw.get(key)
            if isinstance(values, list):
                return _normalize_codes(values)
    return []


def _load_parquet_codes(path: Path, column: str = "code") -> list[str]:
    if not path.exists():
        return []
    frame = pd.read_parquet(path, columns=[column])
    if column not in frame.columns:
        return []
    return _normalize_codes(frame[column].dropna().tolist())


def _rank_codes_by_frequency(path: Path, column: str = "code") -> list[str]:
    if not path.exists():
        return []
    frame = pd.read_parquet(path, columns=[column])
    if column not in frame.columns:
        return []
    counts = Counter(_normalize_codes(frame[column].dropna().tolist()))
    return [code for code, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def _load_sources() -> list[CandidateSource]:
    analyst_path = ANALYST_EXPECTATIONS_DIR / "analyst_ratings_cninfo.parquet"
    margin_path = FLOW_SIGNALS_DIR / "margin_details.parquet"
    sources = [
        CandidateSource("current_stock_list_seed", STOCK_LIST, _load_json_codes(STOCK_LIST)),
        CandidateSource("industry_fixed", INDUSTRY_PATH, _load_parquet_codes(INDUSTRY_PATH)),
        CandidateSource("analyst_ratings_frequency_ranked", analyst_path, _rank_codes_by_frequency(analyst_path)),
        CandidateSource("margin_details_snapshot", margin_path, _load_parquet_codes(margin_path)),
    ]
    return sources


def _filter_candidates(codes: list[str], excluded: set[str]) -> list[str]:
    return [
        code
        for code in _unique_preserve_order(codes)
        if code not in excluded and not code.startswith(("4", "8", "9"))
    ]


def _sha256_json_list(codes: list[str]) -> str:
    payload = json.dumps(codes, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def main() -> None:
    args = _parse_args()
    if args.target_size < 1:
        raise ValueError("target_size must be positive")

    data_dir = PROJECT_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    csi300_path = _resolve(str(args.csi300_components), PROJECT_DIR)
    csi300_codes = set(_load_json_codes(csi300_path))
    excluded_large_caps = set(csi300_codes).union(EXPLICIT_LARGE_CAP_BLACKLIST)
    if not csi300_codes:
        raise RuntimeError(f"missing CSI300 component evidence: {csi300_path}")

    sources = _load_sources()
    selected: list[str] = []
    selected_by_source: dict[str, int] = {}
    candidate_counts: dict[str, int] = {}
    excluded_counts: dict[str, int] = {}
    seen: set[str] = set()

    for source in sources:
        filtered = _filter_candidates(source.codes, excluded_large_caps)
        candidate_counts[source.name] = len(set(source.codes))
        excluded_counts[source.name] = len(set(source.codes).intersection(excluded_large_caps))
        before = len(selected)
        for code in filtered:
            if code in seen:
                continue
            selected.append(code)
            seen.add(code)
            if len(selected) >= args.target_size:
                break
        selected_by_source[source.name] = len(selected) - before
        if len(selected) >= args.target_size:
            break

    if len(selected) < args.target_size:
        raise RuntimeError(
            f"only {len(selected)} non-large-cap candidates available; need {args.target_size}"
        )

    selected = selected[: args.target_size]
    universe_sha256 = _sha256_json_list(selected)
    output_list_path = _resolve(str(args.output_list), data_dir)
    output_json_path = _resolve(str(args.output_json), RESULTS_DIR)
    output_list_path.write_text(
        json.dumps(selected, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    csi300_overlap = sorted(set(selected).intersection(csi300_codes))
    report = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V26-non-largecap-universe",
        "research_only": True,
        "production_ready": False,
        "target_size": args.target_size,
        "selected_count": len(selected),
        "universe_path": str(output_list_path),
        "universe_sha256": universe_sha256,
        "first_codes": selected[:20],
        "last_codes": selected[-20:],
        "large_cap_exclusion": {
            "method": "CSI300 component evidence plus explicit large-cap blacklist",
            "csi300_evidence_path": str(csi300_path),
            "csi300_component_count": len(csi300_codes),
            "explicit_large_cap_blacklist_count": len(EXPLICIT_LARGE_CAP_BLACKLIST),
            "selected_csi300_overlap_count": len(csi300_overlap),
            "selected_csi300_overlap": csi300_overlap,
            "provider_gap_note": (
                "CSI300 evidence is accepted as a conservative local exclusion list for "
                "research planning. Production still requires an approved security master "
                "and market-cap/float/ST/suspension filters from an entitled provider."
            ),
        },
        "source_counts": candidate_counts,
        "selected_by_source": selected_by_source,
        "excluded_large_caps_by_source": excluded_counts,
        "coverage_intent": {
            "northbound_stock_holding_min_target_symbols": args.target_size,
            "intraday_microstructure_min_target_symbols": args.target_size,
            "margin_short_min_target_symbols": args.target_size,
            "borrow_availability_min_target_symbols": args.target_size,
        },
        "production_blockers": [
            "local universe is built from available research files, not an approved security master",
            "large-cap exclusion lacks full production market-cap/float evidence",
            "ST, suspension and listing-status filtering require an entitled provider before trading",
            "all PIT panel backfills must use this universe explicitly through QUANT_STOCK_LIST",
        ],
    }
    output_json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        "V26 non-largecap universe: "
        f"selected={len(selected)} target={args.target_size} "
        f"sha256={universe_sha256[:12]} csi300_overlap={len(csi300_overlap)}",
        flush=True,
    )
    print(f"Wrote {output_list_path}", flush=True)
    print(f"Wrote {output_json_path}", flush=True)


if __name__ == "__main__":
    main()
