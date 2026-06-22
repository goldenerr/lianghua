#!/usr/bin/env python3
"""Score V27 strategy and production-readiness evidence.

This is an evidence gate, not an optimizer. It deliberately fails closed when
metrics, PIT data, external attestations, or paper-trading proof are missing.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _paths import RESULTS_DIR

DEFAULT_OUTPUT_JSON = RESULTS_DIR / "quant_v27_strategy_readiness.json"
DEFAULT_OUTPUT_CSV = RESULTS_DIR / "quant_v27_strategy_readiness_candidates.csv"
DEFAULT_PIT_PANEL_JSON = RESULTS_DIR / "quant_pit_alpha_panel_readiness_v27_provider_qualified_2000.json"
DEFAULT_EXTERNAL_GATE_JSON = RESULTS_DIR / "quant_external_evidence_gate_v26.json"
DEFAULT_LOCAL_EVIDENCE_JSON = RESULTS_DIR / "quant_v27_local_preprod_evidence.json"
DEFAULT_LOCAL_GRID_JSON = RESULTS_DIR / "quant_v27_local_paper_param_grid.json"
DEFAULT_V29_ROBUSTNESS_JSON = RESULTS_DIR / "quant_v29_robustness.json"
DEFAULT_V29_V31_EXECUTION_ROBUSTNESS_JSON = (
    RESULTS_DIR / "quant_v29_robustness_v31_execution_constrained.json"
)
DEFAULT_V29_20Y_V31_EXECUTION_ROBUSTNESS_JSON = (
    RESULTS_DIR / "quant_v29_robustness_20y_v31_execution_constrained.json"
)
DEFAULT_V29_LAUNCH_PACKAGE_JSON = RESULTS_DIR / "quant_v29_paper_trading_launch_package.json"
DEFAULT_V30_PRODUCTION_DATA_GATE_JSON = RESULTS_DIR / "quant_v30_production_data_gate.json"
DEFAULT_V31_FREE_DATA_QUALITY_GATE_JSON = RESULTS_DIR / "quant_v31_free_data_quality_gate.json"

PRODUCTION_THRESHOLDS = {
    "full_sharpe_min": 1.20,
    "oos_sharpe_min": 0.84,
    "max_drawdown_min": -0.15,
    "win_rate_min": 0.40,
    "sharpe_decay_max": 0.30,
    "wf_folds_min": 5,
}

INDUSTRY_LEADING_THRESHOLDS = {
    "full_sharpe_min": 2.00,
    "oos_sharpe_min": 1.50,
    "max_drawdown_min": -0.10,
    "win_rate_min": 0.50,
    "sharpe_decay_max": 0.15,
    "wf_folds_min": 5,
    "min_oos_sharpe_min": 0.0,
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument("--pit-panel-json", default=str(DEFAULT_PIT_PANEL_JSON))
    parser.add_argument("--external-gate-json", default=str(DEFAULT_EXTERNAL_GATE_JSON))
    parser.add_argument("--local-evidence-json", default=str(DEFAULT_LOCAL_EVIDENCE_JSON))
    parser.add_argument("--local-grid-json", default=str(DEFAULT_LOCAL_GRID_JSON))
    parser.add_argument("--v29-robustness-json", default=str(DEFAULT_V29_ROBUSTNESS_JSON))
    parser.add_argument(
        "--v29-v31-execution-robustness-json",
        default=str(DEFAULT_V29_V31_EXECUTION_ROBUSTNESS_JSON),
    )
    parser.add_argument(
        "--v29-20y-v31-execution-robustness-json",
        default=str(DEFAULT_V29_20Y_V31_EXECUTION_ROBUSTNESS_JSON),
    )
    parser.add_argument("--v29-launch-package-json", default=str(DEFAULT_V29_LAUNCH_PACKAGE_JSON))
    parser.add_argument(
        "--v30-production-data-gate-json",
        default=str(DEFAULT_V30_PRODUCTION_DATA_GATE_JSON),
    )
    parser.add_argument(
        "--v31-free-data-quality-gate-json",
        default=str(DEFAULT_V31_FREE_DATA_QUALITY_GATE_JSON),
    )
    return parser.parse_args()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _wf_stats(wf: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(wf, dict):
        return {
            "avg_is_sharpe": None,
            "avg_oos_sharpe": None,
            "sharpe_decay": None,
            "min_oos_sharpe": None,
            "max_oos_drawdown": None,
            "wf_folds": 0,
        }
    folds = wf.get("folds")
    fold_list = folds if isinstance(folds, list) else []
    oos_values = [_num(item.get("oos")) for item in fold_list if isinstance(item, dict)]
    oos_values = [value for value in oos_values if value is not None]
    mdd_values = [_num(item.get("mdd")) for item in fold_list if isinstance(item, dict)]
    mdd_values = [value for value in mdd_values if value is not None]
    return {
        "avg_is_sharpe": _num(wf.get("avg_is_sharpe")),
        "avg_oos_sharpe": _num(wf.get("avg_oos_sharpe")),
        "sharpe_decay": _num(wf.get("sharpe_decay")),
        "min_oos_sharpe": min(oos_values) if oos_values else None,
        "max_oos_drawdown": min(mdd_values) if mdd_values else None,
        "wf_folds": len(fold_list),
    }


def _candidate_score(candidate: dict[str, Any]) -> float:
    full_sharpe = candidate.get("full_sharpe")
    oos_sharpe = candidate.get("avg_oos_sharpe")
    max_drawdown = candidate.get("max_drawdown")
    win_rate = candidate.get("win_rate")
    decay = candidate.get("sharpe_decay")
    min_oos = candidate.get("min_oos_sharpe")
    score = 0.0
    if full_sharpe is not None:
        score += _clamp(float(full_sharpe) / 1.20, -1.0, 2.0) * 25.0
    if oos_sharpe is not None:
        score += _clamp(float(oos_sharpe) / 1.20, -1.0, 2.0) * 30.0
    if max_drawdown is not None:
        score += (1.0 - _clamp(abs(float(max_drawdown)) / 0.25, 0.0, 1.5)) * 15.0
    if win_rate is not None:
        score += _clamp(float(win_rate) / 0.50, 0.0, 1.5) * 10.0
    if decay is not None:
        score += (1.0 - _clamp(float(decay), -0.5, 1.0)) * 10.0
    if min_oos is not None:
        score += 10.0 if float(min_oos) >= 0.0 else -10.0
    return round(score, 4)


def _gate_reasons(candidate: dict[str, Any], thresholds: dict[str, float]) -> list[str]:
    reasons: list[str] = []
    checks = [
        ("full_sharpe", ">=", thresholds["full_sharpe_min"]),
        ("avg_oos_sharpe", ">=", thresholds["oos_sharpe_min"]),
        ("max_drawdown", ">=", thresholds["max_drawdown_min"]),
        ("win_rate", ">=", thresholds["win_rate_min"]),
        ("sharpe_decay", "<=", thresholds["sharpe_decay_max"]),
    ]
    for key, op, threshold in checks:
        value = candidate.get(key)
        if value is None:
            reasons.append(f"{key}:missing")
            continue
        failed = value < threshold if op == ">=" else value > threshold
        if failed:
            reasons.append(f"{key}:{value} {op} {threshold} failed")
    folds = int(candidate.get("wf_folds") or 0)
    if folds < int(thresholds["wf_folds_min"]):
        reasons.append(f"wf_folds:{folds} < {int(thresholds['wf_folds_min'])}")
    min_oos_threshold = thresholds.get("min_oos_sharpe_min")
    min_oos = candidate.get("min_oos_sharpe")
    if min_oos_threshold is not None and (min_oos is None or min_oos < min_oos_threshold):
        reasons.append(f"min_oos_sharpe:{min_oos} >= {min_oos_threshold} failed")
    return reasons


def _build_candidate(
    *,
    source: Path,
    name: str,
    version: str,
    full: dict[str, Any] | None,
    wf: dict[str, Any] | None,
    candidate_type: str,
    production_comparable: bool = True,
) -> dict[str, Any]:
    full = full if isinstance(full, dict) else {}
    candidate = {
        "source": str(source),
        "name": name,
        "version": version,
        "candidate_type": candidate_type,
        "production_comparable": production_comparable,
        "full_sharpe": _num(full.get("sharpe_ratio")),
        "annual_return": _num(full.get("annual_return")),
        "annual_volatility": _num(full.get("annual_volatility")),
        "max_drawdown": _num(full.get("max_drawdown")),
        "win_rate": _num(full.get("win_rate")),
        "total_return": _num(full.get("total_return")),
        "n_days": _num(full.get("n_days")),
    }
    candidate.update(_wf_stats(wf))
    candidate["score"] = _candidate_score(candidate)
    prod_reasons = _gate_reasons(candidate, PRODUCTION_THRESHOLDS)
    leading_reasons = _gate_reasons(candidate, INDUSTRY_LEADING_THRESHOLDS)
    if not production_comparable:
        prod_reasons.append("not a full production-comparable candidate")
        leading_reasons.append("not a full production-comparable candidate")
    candidate["production_minimum_passes"] = not prod_reasons
    candidate["production_minimum_failures"] = prod_reasons
    candidate["industry_leading_passes"] = not leading_reasons
    candidate["industry_leading_failures"] = leading_reasons
    return candidate


def _extract_candidates(path: Path) -> list[dict[str, Any]]:
    data = _read_json(path)
    candidates: list[dict[str, Any]] = []

    def add_from_mapping(item: dict[str, Any], name: str, candidate_type: str) -> None:
        version = str(item.get("version") or data.get("version") or path.stem)
        candidates.append(
            _build_candidate(
                source=path,
                name=name,
                version=version,
                full=item.get("full"),
                wf=item.get("wf"),
                candidate_type=candidate_type,
                production_comparable=bool(item.get("production_comparable", True)),
            )
        )

    if isinstance(data, dict):
        if isinstance(data.get("full"), dict):
            add_from_mapping(data, str(data.get("label") or data.get("version") or path.stem), "research_finalist")
        if isinstance(data.get("scenarios"), dict):
            for scenario_name, scenario in data["scenarios"].items():
                if isinstance(scenario, dict):
                    add_from_mapping(scenario, f"{path.stem}:{scenario_name}", "validation_scenario")
        if isinstance(data.get("results"), list):
            for idx, item in enumerate(data["results"]):
                if isinstance(item, dict):
                    name = str(item.get("name") or item.get("label") or f"{path.stem}:result_{idx}")
                    add_from_mapping(item, name, "grid_result")
        if isinstance(data.get("focus_trace"), dict):
            stats = data["focus_trace"].get("stats")
            candidates.append(
                _build_candidate(
                    source=path,
                    name=str(data.get("candidate") or f"{path.stem}:focus_trace"),
                    version=str(data.get("version") or path.stem),
                    full=stats,
                    wf=None,
                    candidate_type="oos_diagnostic_focus_fold",
                    production_comparable=False,
                )
            )
    elif isinstance(data, list):
        for idx, item in enumerate(data):
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("label") or f"{path.stem}:result_{idx}")
                add_from_mapping(item, name, "grid_result")
    return candidates


def _local_paper_candidate(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = _read_json(path)
    paper = data.get("paper_trading_dry_run_local") if isinstance(data, dict) else None
    if not isinstance(paper, dict):
        return None
    return _build_candidate(
        source=path,
        name="V27 local historical paper dry-run",
        version=str(data.get("version") or path.stem),
        full=paper,
        wf=None,
        candidate_type="local_paper_dry_run",
        production_comparable=False,
    )


def _collect_candidates(local_evidence_path: Path) -> list[dict[str, Any]]:
    patterns = [
        "quant_logic_research_v*.json",
        "quant_logic_validation_v*.json",
    ]
    candidates: list[dict[str, Any]] = []
    for pattern in patterns:
        for path in sorted(RESULTS_DIR.glob(pattern)):
            try:
                candidates.extend(_extract_candidates(path))
            except Exception as exc:
                candidates.append(
                    {
                        "source": str(path),
                        "name": path.stem,
                        "candidate_type": "parse_error",
                        "production_comparable": False,
                        "score": -999.0,
                        "production_minimum_passes": False,
                        "production_minimum_failures": [f"parse_error:{exc}"],
                        "industry_leading_passes": False,
                        "industry_leading_failures": [f"parse_error:{exc}"],
                    }
                )
    local_candidate = _local_paper_candidate(local_evidence_path)
    if local_candidate:
        candidates.append(local_candidate)
    return sorted(candidates, key=lambda item: float(item.get("score") or -999.0), reverse=True)


def _panel_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "blockers": [f"missing {path}"]}
    data = _read_json(path)
    summary = data.get("summary", {}) if isinstance(data, dict) else {}
    return {
        "exists": True,
        "production_data_ready": bool(data.get("production_data_ready")),
        "research_ready_panels": summary.get("research_ready_panels"),
        "total_panels": summary.get("total_panels"),
        "production_ready_panels": summary.get("production_ready_panels"),
        "missing_panels": summary.get("missing_panels", []),
        "snapshot_only_panels": summary.get("snapshot_only_panels", []),
        "coverage_or_history_gaps": summary.get("coverage_or_history_gaps", []),
        "research_ready_external_evidence_missing": summary.get(
            "research_ready_external_evidence_missing", []
        ),
    }


def _external_gate_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "production_ready": False, "blockers": [f"missing {path}"]}
    data = _read_json(path)
    return {
        "exists": True,
        "production_ready": bool(data.get("production_ready")),
        "provided_keys": data.get("provided_keys", []),
        "required_keys": data.get("required_keys", []),
        "missing_or_untrusted_blockers": data.get("missing_or_untrusted_blockers", []),
    }


def _local_evidence_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "blockers": [f"missing {path}"]}
    data = _read_json(path)
    paper = data.get("paper_trading_dry_run_local", {}) if isinstance(data, dict) else {}
    return {
        "exists": True,
        "production_ready": bool(data.get("production_ready")),
        "capacity_passes_local": bool(
            data.get("capacity_benchmark_local", {}).get("passes_local_minimum")
        ),
        "failover_passes_local": bool(data.get("failover_drill_local", {}).get("passes_local_drill")),
        "local_audit_verified": bool(data.get("local_audit_archive", {}).get("verified")),
        "paper_completed_days": paper.get("completed_days"),
        "paper_sharpe": paper.get("sharpe_ratio"),
        "paper_max_drawdown": paper.get("max_drawdown"),
        "paper_price_frame_coverage_passes_research": paper.get("price_frame_coverage_passes_research"),
        "paper_data_coverage_blockers": paper.get("data_coverage_blockers", []),
        "production_blockers": data.get("production_blockers", []),
    }


def _local_grid_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "blockers": [f"missing {path}"]}
    data = _read_json(path)
    best = data.get("best", {}) if isinstance(data, dict) else {}
    return {
        "exists": True,
        "candidate_count": data.get("candidate_count"),
        "passes_production_minimum_probe_count": data.get(
            "passes_production_minimum_probe_count"
        ),
        "best": {
            "name": best.get("name"),
            "sharpe_ratio": best.get("sharpe_ratio"),
            "annual_return": best.get("annual_return"),
            "max_drawdown": best.get("max_drawdown"),
            "total_return": best.get("total_return"),
            "passes_production_minimum_probe": best.get("passes_production_minimum_probe"),
        },
        "production_blockers": data.get("production_blockers", []),
    }


def _v29_robustness_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "robustness_gate_passes": False, "blockers": [f"missing {path}"]}
    data = _read_json(path)
    cases = data.get("cases", []) if isinstance(data, dict) else []
    leading_fail_count = sum(
        1
        for item in cases
        if isinstance(item, dict) and item.get("leading_passes") is False
    )
    return {
        "exists": True,
        "version": data.get("version"),
        "candidate": data.get("candidate"),
        "robustness_gate_passes": bool(data.get("robustness_gate_passes")),
        "robustness_failures": data.get("robustness_failures", []),
        "summary": data.get("summary", {}),
        "trading_constraint_summary": data.get("trading_constraint_summary", {}),
        "case_count": len(cases),
        "leading_case_fail_count": leading_fail_count,
        "production_blockers": data.get("production_blockers", []),
    }


def _v29_launch_package_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "paper_launch_ready": False, "blockers": [f"missing {path}"]}
    data = _read_json(path)
    paper_blockers = data.get("paper_launch_blockers", [])
    production_blockers = data.get("production_release_blockers", [])
    return {
        "exists": True,
        "candidate": data.get("candidate"),
        "paper_launch_ready": bool(data.get("paper_launch_ready")),
        "production_ready": bool(data.get("production_ready")),
        "production_release_evidence_ready": bool(data.get("production_release_evidence_ready")),
        "launch_mode": data.get("launch_mode"),
        "missing_evidence_for_paper_start": data.get("missing_evidence_for_paper_start", []),
        "missing_evidence_for_full_production": data.get(
            "missing_evidence_for_full_production", []
        ),
        "paper_launch_blocker_count": len(paper_blockers) if isinstance(paper_blockers, list) else None,
        "production_release_blocker_count": (
            len(production_blockers) if isinstance(production_blockers, list) else None
        ),
    }


def _v30_production_data_gate_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "production_data_ready": False, "blockers": [f"missing {path}"]}
    data = _read_json(path)
    summary = data.get("summary", {}) if isinstance(data, dict) else {}
    external = data.get("external_gate_inputs", {}) if isinstance(data, dict) else {}
    audit = external.get("twenty_year_audit", {}) if isinstance(external, dict) else {}
    diagnostics = data.get("diagnostics", {}) if isinstance(data, dict) else {}
    intraday = diagnostics.get("existing_intraday_microstructure", {}) if isinstance(diagnostics, dict) else {}
    return {
        "exists": True,
        "production_data_ready": bool(data.get("production_data_ready")),
        "total_requirements": summary.get("total_requirements"),
        "local_coverage_ready_count": summary.get("local_coverage_ready_count"),
        "evidence_ready_count": summary.get("evidence_ready_count"),
        "production_ready_count": summary.get("production_ready_count"),
        "requirement_blocker_count": summary.get("requirement_blocker_count"),
        "evidence_failure_count": summary.get("evidence_failure_count"),
        "cross_gate_blocker_count": summary.get("cross_gate_blocker_count"),
        "can_claim_20y_full_universe_production_validation": audit.get(
            "can_claim_20y_full_universe_production_validation"
        ),
        "diagnostic_start_after_warmup": audit.get("diagnostic_start_after_warmup"),
        "production_full_universe_start_after_warmup": audit.get(
            "production_full_universe_start_after_warmup"
        ),
        "existing_intraday_unique_dates": intraday.get("unique_dates"),
        "existing_intraday_unique_entities": intraday.get("unique_entities"),
        "blockers": data.get("blockers", []),
    }


def _v31_free_data_quality_gate_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "exists": False,
            "free_research_ready": False,
            "production_data_ready": False,
            "blockers": [f"missing {path}"],
        }
    data = _read_json(path)
    summary = data.get("summary", {}) if isinstance(data, dict) else {}
    return {
        "exists": True,
        "free_research_ready": bool(data.get("free_research_ready")),
        "production_data_ready": bool(data.get("production_data_ready")),
        "research_ready_count": summary.get("research_ready_count"),
        "total_requirements": summary.get("total_requirements"),
        "research_blocker_count": summary.get("research_blocker_count"),
        "production_blocker_count": summary.get("production_blocker_count"),
        "build_symbols_loaded": summary.get("build_symbols_loaded"),
        "build_unique_trading_dates": summary.get("build_unique_trading_dates"),
        "research_blockers": data.get("research_blockers", []),
        "production_blockers": data.get("production_blockers", []),
    }


def _write_csv(path: Path, candidates: list[dict[str, Any]]) -> None:
    fields = [
        "score",
        "name",
        "version",
        "candidate_type",
        "production_comparable",
        "production_minimum_passes",
        "industry_leading_passes",
        "full_sharpe",
        "avg_oos_sharpe",
        "min_oos_sharpe",
        "sharpe_decay",
        "max_drawdown",
        "win_rate",
        "annual_return",
        "total_return",
        "wf_folds",
        "source",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for candidate in candidates:
            writer.writerow({field: candidate.get(field) for field in fields})


def main() -> None:
    args = _parse_args()
    output_json = Path(args.output_json)
    output_csv = Path(args.output_csv)
    pit_panel_path = Path(args.pit_panel_json)
    external_gate_path = Path(args.external_gate_json)
    local_evidence_path = Path(args.local_evidence_json)
    local_grid_path = Path(args.local_grid_json)
    v29_robustness_path = Path(args.v29_robustness_json)
    v29_v31_execution_robustness_path = Path(args.v29_v31_execution_robustness_json)
    v29_20y_v31_execution_robustness_path = Path(args.v29_20y_v31_execution_robustness_json)
    v29_launch_package_path = Path(args.v29_launch_package_json)
    v30_production_data_gate_path = Path(args.v30_production_data_gate_json)
    v31_free_data_quality_gate_path = Path(args.v31_free_data_quality_gate_json)

    candidates = _collect_candidates(local_evidence_path)
    best_candidate = candidates[0] if candidates else None
    best_source = str(best_candidate.get("source", "")) if isinstance(best_candidate, dict) else ""
    best_name = str(best_candidate.get("name", "")) if isinstance(best_candidate, dict) else ""
    best_is_v29_price_only = (
        "quant_logic_research_v29_portfolio_layer" in best_source
        and "alt" not in best_name.lower()
        and bool(best_candidate.get("production_comparable")) if isinstance(best_candidate, dict) else False
    )
    production_candidates = [
        item for item in candidates if item.get("production_comparable") and item.get("production_minimum_passes")
    ]
    leading_candidates = [
        item for item in candidates if item.get("production_comparable") and item.get("industry_leading_passes")
    ]
    panel_summary = _panel_summary(pit_panel_path)
    external_summary = _external_gate_summary(external_gate_path)
    local_summary = _local_evidence_summary(local_evidence_path)
    local_grid_summary = _local_grid_summary(local_grid_path)
    v29_robustness_summary = _v29_robustness_summary(v29_robustness_path)
    v29_v31_execution_robustness_summary = _v29_robustness_summary(
        v29_v31_execution_robustness_path
    )
    v29_20y_v31_execution_robustness_summary = _v29_robustness_summary(
        v29_20y_v31_execution_robustness_path
    )
    v29_launch_package_summary = _v29_launch_package_summary(v29_launch_package_path)
    v30_production_data_gate_summary = _v30_production_data_gate_summary(
        v30_production_data_gate_path
    )
    v31_free_data_quality_gate_summary = _v31_free_data_quality_gate_summary(
        v31_free_data_quality_gate_path
    )

    blockers: list[str] = []
    if not production_candidates:
        blockers.append("no production-comparable strategy candidate passes production minimum metrics")
    if not leading_candidates:
        blockers.append("no production-comparable strategy candidate passes industry-leading metrics")
    if not panel_summary.get("production_data_ready") and not best_is_v29_price_only:
        blockers.append("PIT alpha panel is not production-data-ready")
    if not external_summary.get("production_ready"):
        blockers.append("external production evidence gate is not ready")
    v29_summary = v29_robustness_summary.get("summary", {})
    v29_recent_90_sharpe = _num(v29_summary.get("recent_90_sharpe")) if isinstance(v29_summary, dict) else None
    recommended_candidate = best_candidate
    recommended_reason = "highest score"
    v31_20y_execution_candidate_name = v29_20y_v31_execution_robustness_summary.get("candidate")
    if (
        v29_20y_v31_execution_robustness_summary.get("robustness_gate_passes")
        and v31_20y_execution_candidate_name
    ):
        robust_matches = [
            item
            for item in candidates
            if item.get("name") == v31_20y_execution_candidate_name
            and "20y_v31_execution_constrained" in str(item.get("source", ""))
        ]
        if robust_matches:
            recommended_candidate = robust_matches[0]
            recommended_reason = "selected by 20y V31 execution-constrained robustness gate"
    v31_execution_candidate_name = v29_v31_execution_robustness_summary.get("candidate")
    if (
        recommended_reason == "highest score"
        and
        v29_v31_execution_robustness_summary.get("robustness_gate_passes")
        and v31_execution_candidate_name
    ):
        robust_matches = [
            item
            for item in candidates
            if item.get("name") == v31_execution_candidate_name
            and "v31_execution_constrained" in str(item.get("source", ""))
        ]
        if robust_matches:
            recommended_candidate = robust_matches[0]
            recommended_reason = "selected by V31 execution-constrained robustness gate"
    robustness_candidate_name = v29_robustness_summary.get("candidate")
    if (
        recommended_reason == "highest score"
        and v29_robustness_summary.get("robustness_gate_passes")
        and robustness_candidate_name
    ):
        robust_matches = [item for item in candidates if item.get("name") == robustness_candidate_name]
        if robust_matches:
            recommended_candidate = robust_matches[0]
            recommended_reason = "selected by V29 robustness gate"

    if not local_summary.get("paper_price_frame_coverage_passes_research"):
        blockers.append("V27 local paper dry-run daily price coverage is below research threshold")
    if v29_recent_90_sharpe is not None:
        if v29_recent_90_sharpe < 0.0:
            blockers.append("V29 recent 90-day local robustness Sharpe is negative")
    elif local_summary.get("paper_sharpe") is not None and float(local_summary["paper_sharpe"]) < 1.2:
        blockers.append("V27 local 90-day historical paper dry-run Sharpe is below production threshold")
    if local_grid_summary.get("passes_production_minimum_probe_count") == 0 and not production_candidates:
        blockers.append("V27 local parameter grid has no production-minimum probe candidate")
    if leading_candidates and not v29_robustness_summary.get("robustness_gate_passes"):
        blockers.append("V29 leading candidate robustness gate is not passed")
    if leading_candidates and not v29_v31_execution_robustness_summary.get("robustness_gate_passes"):
        blockers.append("V29 V31 execution-constrained robustness gate is not passed")
    if leading_candidates and not v29_20y_v31_execution_robustness_summary.get("robustness_gate_passes"):
        blockers.append("V29 20y V31 execution-constrained robustness gate is not passed")
    if leading_candidates and not v29_launch_package_summary.get("paper_launch_ready"):
        blockers.append("V29 approved paper-trading launch package is not ready")
    if not v30_production_data_gate_summary.get("production_data_ready"):
        blockers.append("V30 production historical data and execution gate is not ready")

    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V27-strategy-readiness-scorecard",
        "research_only": True,
        "production_ready": False,
        "strategy_gate_passes": bool(production_candidates),
        "industry_leading_candidate_exists": bool(leading_candidates),
        "thresholds": {
            "production": PRODUCTION_THRESHOLDS,
            "industry_leading": INDUSTRY_LEADING_THRESHOLDS,
        },
        "candidate_count": len(candidates),
        "best_candidate": best_candidate,
        "recommended_candidate": recommended_candidate,
        "recommended_candidate_reason": recommended_reason,
        "top_candidates": candidates[:15],
        "production_candidates": production_candidates,
        "industry_leading_candidates": leading_candidates,
        "data_scope_summary": {
            "best_candidate_is_v29_price_only": best_is_v29_price_only,
            "pit_alpha_panel_blocker_applies_to_best_candidate": bool(
                not panel_summary.get("production_data_ready") and not best_is_v29_price_only
            ),
            "note": (
                "V29 price-only candidate does not use archived/snapshot alt panels or borrow data; "
                "production provider entitlement remains enforced by the external evidence gate."
                if best_is_v29_price_only
                else "Best candidate still depends on the general PIT alpha panel readiness gate."
            ),
        },
        "pit_panel_summary": panel_summary,
        "external_gate_summary": external_summary,
        "local_preprod_evidence_summary": local_summary,
        "local_parameter_grid_summary": local_grid_summary,
        "v29_robustness_summary": v29_robustness_summary,
        "v29_v31_execution_robustness_summary": v29_v31_execution_robustness_summary,
        "v29_20y_v31_execution_robustness_summary": v29_20y_v31_execution_robustness_summary,
        "v29_launch_package_summary": v29_launch_package_summary,
        "v30_production_data_gate_summary": v30_production_data_gate_summary,
        "v31_free_data_quality_gate_summary": v31_free_data_quality_gate_summary,
        "blockers": blockers,
        "next_research_actions": [
            "backfill V27 daily OHLCV to >=1900/2000 symbols before trusting 2000-stock paper validation",
            "replace snapshot-only alt panels with paid PIT history or daily immutable snapshot accumulation",
            "design market-neutral and crisis-alpha sleeves with real borrow/futures/ETF execution data",
            "rerun WF/Purged CV on provider-qualified universe after PIT and OHLCV coverage pass",
        ],
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(output_csv, candidates)
    print(f"Wrote {output_json}")
    print(f"Wrote {output_csv}")
    if candidates:
        best = candidates[0]
        print(
            "Best candidate: "
            f"{best.get('name')} score={best.get('score')} "
            f"full_sharpe={best.get('full_sharpe')} "
            f"oos={best.get('avg_oos_sharpe')} "
            f"prod_pass={best.get('production_minimum_passes')} "
            f"leading_pass={best.get('industry_leading_passes')}"
        )
    print(f"Blockers: {len(blockers)}")


if __name__ == "__main__":
    main()
