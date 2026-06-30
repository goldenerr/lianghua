#!/usr/bin/env python3
"""Fail-closed validation for the V29 daily bucket-aware stress flag.

This script performs the required post-simulation checks before any strategy code
could even be considered: look-ahead audit, monthly distribution coverage, and
walk-forward validation evidence. It intentionally separates a positive May 2026
attribution result from production readiness.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from _paths import RESULTS_DIR

DEFAULT_SIMULATION = RESULTS_DIR / "quant_v29_daily_bucket_aware_flag_simulation.json"
DEFAULT_RANDOM = RESULTS_DIR / "quant_v29_daily_bucket_aware_random_baseline.json"
DEFAULT_HYPOTHESIS = RESULTS_DIR / "quant_v29_bucket_risk_flag_hypothesis.json"
DEFAULT_OUTPUT = RESULTS_DIR / "quant_v29_daily_bucket_aware_validation.json"
DEFAULT_REPORT = Path("docs/research/quant_v29_daily_bucket_aware_validation.md")
REQUIRED_STRESS_YEARS = {2018, 2020, 2022, 2026}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulation-json", default=str(DEFAULT_SIMULATION))
    parser.add_argument("--random-baseline-json", default=str(DEFAULT_RANDOM))
    parser.add_argument("--hypothesis-json", default=str(DEFAULT_HYPOTHESIS))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--report-md", default=str(DEFAULT_REPORT))
    return parser.parse_args()


def _safe_float(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def _compound(values: pd.Series) -> float:
    total = 1.0
    for value in pd.to_numeric(values, errors="coerce").fillna(0.0):
        total *= 1.0 + _safe_float(value)
    return total - 1.0


def audit_lookahead(simulation: dict[str, Any], hypothesis: dict[str, Any]) -> dict[str, Any]:
    records = pd.DataFrame(simulation.get("simulated_daily_records", []))
    blockers: list[str] = []
    params = simulation.get("simulation_parameters", {})
    lagged_mechanics = bool(params.get("lagged_inputs_only"))
    if not lagged_mechanics:
        blockers.append("simulation_not_marked_lagged_inputs_only")

    if records.empty:
        blockers.append("no_simulated_daily_records")
        flagged_delta_ok = False
        cap_ok = False
    else:
        return_series = (
            records["return"] if "return" in records else pd.Series(0.0, index=records.index)
        )
        delta_series = (
            records["bucket_aware_delta"]
            if "bucket_aware_delta" in records
            else pd.Series(0.0, index=records.index)
        )
        records["return"] = pd.to_numeric(return_series, errors="coerce").fillna(0.0)
        records["bucket_aware_delta"] = pd.to_numeric(delta_series, errors="coerce").fillna(0.0)
        records["bucket_aware_flag"] = records.get(
            "bucket_aware_flag", pd.Series(False, index=records.index)
        ).astype(bool)
        flagged_delta_ok = bool(
            (
                (records["bucket_aware_delta"] <= 0.0)
                | ((records["bucket_aware_flag"]) & (records["return"] < 0.0))
            ).all()
        )
        cap_ok = bool(
            (
                records["bucket_aware_delta"]
                <= records["return"].abs() * _safe_float(params.get("max_daily_delta_fraction"))
                + 1e-12
            ).all()
        )
        if not flagged_delta_ok:
            blockers.append("delta_applied_without_flagged_negative_day")
        if not cap_ok:
            blockers.append("delta_exceeds_max_daily_delta_fraction")

    source_month = str(hypothesis.get("source_reports", {}).get("target_month", ""))
    target_month = str(simulation.get("target_month", ""))
    posthoc_blocker = bool(source_month and target_month and source_month == target_month)
    if posthoc_blocker:
        blockers.append("hypothesis_inputs_derived_from_same_target_month")

    return {
        "passes": not blockers,
        "lagged_mechanics_pass": bool(lagged_mechanics and flagged_delta_ok and cap_ok),
        "posthoc_hypothesis_source_blocker": posthoc_blocker,
        "source_month": source_month,
        "target_month": target_month,
        "blockers": blockers,
        "interpretation": (
            "mechanical lagging/cap checks pass, but same-month hypothesis derivation blocks a clean look-ahead pass"
            if posthoc_blocker and lagged_mechanics
            else "see blockers"
        ),
    }


def monthly_distribution_check(simulation: dict[str, Any]) -> dict[str, Any]:
    records = pd.DataFrame(simulation.get("simulated_daily_records", []))
    blockers: list[str] = []
    if records.empty:
        return {
            "passes": False,
            "unique_month_count": 0,
            "blockers": ["no_simulated_daily_records"],
        }
    records["date"] = pd.to_datetime(records["date"], errors="coerce")
    records = records.dropna(subset=["date"])
    records["month"] = records["date"].dt.to_period("M").astype(str)
    records["year"] = records["date"].dt.year
    rows: list[dict[str, Any]] = []
    for (candidate, month), group in records.groupby(["candidate", "month"]):
        baseline = _compound(group["return"])
        simulated = _compound(group["simulated_return"])
        rows.append(
            {
                "candidate": str(candidate),
                "month": str(month),
                "baseline_compound_return": round(float(baseline), 6),
                "simulated_compound_return": round(float(simulated), 6),
                "simulated_minus_baseline": round(float(simulated - baseline), 6),
                "n_days": int(len(group)),
            }
        )
    unique_months = sorted(records["month"].dropna().unique().tolist())
    years_present = set(int(year) for year in records["year"].dropna().unique().tolist())
    if len(unique_months) < 24:
        blockers.append("insufficient_month_coverage")
    missing_years = sorted(REQUIRED_STRESS_YEARS - years_present)
    if missing_years:
        blockers.append("missing_required_stress_years:" + ",".join(map(str, missing_years)))
    return {
        "passes": not blockers,
        "unique_month_count": int(len(unique_months)),
        "months": unique_months,
        "required_stress_years": sorted(REQUIRED_STRESS_YEARS),
        "missing_stress_years": missing_years,
        "monthly_rows": rows,
        "blockers": blockers,
    }


def walk_forward_validation_check(
    simulation: dict[str, Any], random_baseline: dict[str, Any]
) -> dict[str, Any]:
    fold_records = simulation.get("walk_forward_fold_records", []) or simulation.get("wf_folds", [])
    blockers: list[str] = []
    if not fold_records:
        blockers.append("no_walk_forward_fold_records")
    random_summaries = {
        str(item.get("candidate")): item for item in random_baseline.get("summaries", [])
    }
    not_random_validated = [
        candidate
        for candidate, item in random_summaries.items()
        if not bool(item.get("beats_random_median"))
    ]
    if not_random_validated:
        blockers.append("candidate_failed_random_baseline:" + ",".join(not_random_validated))
    if fold_records:
        valid_folds = [item for item in fold_records if isinstance(item, dict)]
        min_oos = min((_safe_float(item.get("oos_sharpe")) for item in valid_folds), default=0.0)
        if len(valid_folds) < 5:
            blockers.append("too_few_walk_forward_folds")
        if min_oos < 0.0:
            blockers.append("negative_oos_fold")
    return {
        "passes": not blockers,
        "fold_count": int(len(fold_records)),
        "random_baseline_candidates": random_summaries,
        "blockers": blockers,
        "interpretation": "No historical fold evidence exists in the current bucket-aware artifact; cannot validate WF OOS.",
    }


def build_full_report(
    simulation: dict[str, Any], random_baseline: dict[str, Any], hypothesis: dict[str, Any]
) -> dict[str, Any]:
    lookahead = audit_lookahead(simulation, hypothesis)
    monthly = monthly_distribution_check(simulation)
    wf = walk_forward_validation_check(simulation, random_baseline)
    blockers = []
    for section, payload in [("lookahead", lookahead), ("monthly", monthly), ("walk_forward", wf)]:
        blockers.extend([f"{section}:{item}" for item in payload.get("blockers", [])])
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V29-daily-bucket-aware-lookahead-monthly-wf-validation",
        "research_only": True,
        "production_ready": False,
        "not_parameter_tuning": True,
        "can_change_strategy_now": False,
        "target_month": simulation.get("target_month"),
        "lookahead_audit": lookahead,
        "monthly_distribution": monthly,
        "walk_forward_validation": wf,
        "global_decision": {
            "passes": False,
            "can_change_strategy_now": False,
            "can_claim_production_ready": False,
            "blockers": blockers,
            "reason": "Observed May improvement beats random median, but clean look-ahead, multi-month distribution and WF OOS validation are not satisfied.",
        },
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    decision = report["global_decision"]
    lines = [
        "# V29 daily bucket-aware validation",
        "",
        "状态：fail-closed validation；不调参；不改策略；不解除 production blocker。",
        "",
        f"- target_month: {report['target_month']}",
        f"- production_ready: {str(report['production_ready']).lower()}",
        f"- can_change_strategy_now: {str(report['can_change_strategy_now']).lower()}",
        "",
        "## Look-ahead audit",
        "",
        f"- passes: {str(report['lookahead_audit']['passes']).lower()}",
        f"- lagged_mechanics_pass: {str(report['lookahead_audit']['lagged_mechanics_pass']).lower()}",
        f"- posthoc_hypothesis_source_blocker: {str(report['lookahead_audit']['posthoc_hypothesis_source_blocker']).lower()}",
        f"- blockers: {report['lookahead_audit']['blockers']}",
        "",
        "## Monthly distribution",
        "",
        f"- passes: {str(report['monthly_distribution']['passes']).lower()}",
        f"- unique_month_count: {report['monthly_distribution']['unique_month_count']}",
        f"- missing_stress_years: {report['monthly_distribution']['missing_stress_years']}",
        f"- blockers: {report['monthly_distribution']['blockers']}",
        "",
        "## Walk-forward validation",
        "",
        f"- passes: {str(report['walk_forward_validation']['passes']).lower()}",
        f"- fold_count: {report['walk_forward_validation']['fold_count']}",
        f"- blockers: {report['walk_forward_validation']['blockers']}",
        "",
        "## Decision",
        "",
        f"- passes: {str(decision['passes']).lower()}",
        f"- blockers: {decision['blockers']}",
        f"- reason: {decision['reason']}",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    simulation = json.loads(Path(args.simulation_json).read_text(encoding="utf-8"))
    random_baseline = json.loads(Path(args.random_baseline_json).read_text(encoding="utf-8"))
    hypothesis = json.loads(Path(args.hypothesis_json).read_text(encoding="utf-8"))
    report = build_full_report(simulation, random_baseline, hypothesis)
    _write_json(Path(args.output_json), report)
    _write_markdown(Path(args.report_md), report)
    print(
        json.dumps(
            {
                "production_ready": False,
                "can_change_strategy_now": False,
                "output": args.output_json,
                "report": args.report_md,
                "lookahead_passes": report["lookahead_audit"]["passes"],
                "monthly_passes": report["monthly_distribution"]["passes"],
                "wf_passes": report["walk_forward_validation"]["passes"],
                "blockers": report["global_decision"]["blockers"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
