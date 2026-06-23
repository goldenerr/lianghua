#!/usr/bin/env python3
"""Validate V40 cross-capital, cost and parameter robustness evidence."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _paths import RESULTS_DIR

DEFAULT_BASE = RESULTS_DIR / "quant_v40_stock_alpha_defensive_basket.json"
DEFAULT_STRESS = RESULTS_DIR / "quant_v40_stock_alpha_defensive_basket_cost_stress.json"
DEFAULT_SENSITIVITY = RESULTS_DIR / "quant_v40_stock_alpha_defensive_basket_sensitivity.json"
DEFAULT_SENSITIVITY_STRESS = (
    RESULTS_DIR / "quant_v40_stock_alpha_defensive_basket_sensitivity_cost_stress.json"
)
DEFAULT_OUTPUT = RESULTS_DIR / "quant_v40_robustness.json"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=str(DEFAULT_BASE))
    parser.add_argument("--stress", default=str(DEFAULT_STRESS))
    parser.add_argument("--sensitivity", default=str(DEFAULT_SENSITIVITY))
    parser.add_argument("--sensitivity-stress", default=str(DEFAULT_SENSITIVITY_STRESS))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--candidate-capital", type=float, default=200000.0)
    parser.add_argument("--candidate-stock-cap", type=float, default=0.07)
    parser.add_argument("--candidate-crisis-cap", type=float, default=0.20)
    parser.add_argument("--min-cross-capital-passes", type=int, default=2)
    parser.add_argument("--min-sensitivity-pass-ratio", type=float, default=0.80)
    parser.add_argument("--max-metric-spread", type=float, default=0.15)
    return parser.parse_args()


def _load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("production_ready") is not False or not isinstance(data.get("results"), list):
        raise RuntimeError(f"invalid V40 research evidence: {path}")
    if not data.get("v39_baseline_parity_passes"):
        raise RuntimeError(f"V40 baseline parity failed: {path}")
    return data


def _matches(row: dict[str, Any], *, stock_cap: float, crisis_cap: float) -> bool:
    scenario = row["scenario"]
    return abs(float(scenario["stock_alpha_cap"]) - stock_cap) <= 1e-12 and abs(
        float(scenario["crisis_cap"]) - crisis_cap
    ) <= 1e-12


def _candidate(
    report: dict[str, Any], *, capital: float, stock_cap: float, crisis_cap: float
) -> dict[str, Any]:
    for row in report["results"]:
        if abs(float(row["scenario"]["capital"]) - capital) <= 1e-9 and _matches(
            row, stock_cap=stock_cap, crisis_cap=crisis_cap
        ):
            return row
    raise RuntimeError(
        f"candidate missing: capital={capital}, stock_cap={stock_cap}, crisis_cap={crisis_cap}"
    )


def _pass_ratio(report: dict[str, Any]) -> float:
    rows = report["results"]
    if not rows:
        return 0.0
    passed = sum(1 for row in rows if row["gates"]["relative_improvement_gate_passes"])
    return float(passed / len(rows))


def _metric_spread(report: dict[str, Any], field: str, center: float) -> float:
    values = [float(row["full"][field]) for row in report["results"]]
    denominator = max(abs(center), 1e-12)
    return max(abs(value - center) for value in values) / denominator if values else float("inf")


def _cross_capital_passes(
    report: dict[str, Any], *, stock_cap: float, crisis_cap: float
) -> tuple[int, list[int]]:
    passing = sorted(
        int(float(row["scenario"]["capital"]))
        for row in report["results"]
        if _matches(row, stock_cap=stock_cap, crisis_cap=crisis_cap)
        and row["gates"]["relative_improvement_gate_passes"]
    )
    return len(passing), passing


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    base = _load(Path(args.base))
    stress = _load(Path(args.stress))
    sensitivity = _load(Path(args.sensitivity))
    sensitivity_stress = _load(Path(args.sensitivity_stress))
    candidate_kwargs = {
        "capital": float(args.candidate_capital),
        "stock_cap": float(args.candidate_stock_cap),
        "crisis_cap": float(args.candidate_crisis_cap),
    }
    base_candidate = _candidate(base, **candidate_kwargs)
    stress_candidate = _candidate(stress, **candidate_kwargs)
    default_ratio = _pass_ratio(sensitivity)
    stress_ratio = _pass_ratio(sensitivity_stress)
    cross_default, cross_default_capitals = _cross_capital_passes(
        base,
        stock_cap=float(args.candidate_stock_cap),
        crisis_cap=float(args.candidate_crisis_cap),
    )
    cross_stress, cross_stress_capitals = _cross_capital_passes(
        stress,
        stock_cap=float(args.candidate_stock_cap),
        crisis_cap=float(args.candidate_crisis_cap),
    )
    default_spreads = {
        field: _metric_spread(sensitivity, field, float(base_candidate["full"][field]))
        for field in ("annual_return", "sharpe_ratio", "max_drawdown")
    }
    stress_spreads = {
        field: _metric_spread(sensitivity_stress, field, float(stress_candidate["full"][field]))
        for field in ("annual_return", "sharpe_ratio", "max_drawdown")
    }
    gates = {
        "candidate_default_relative_passes": bool(
            base_candidate["gates"]["relative_improvement_gate_passes"]
        ),
        "candidate_cost_stress_relative_passes": bool(
            stress_candidate["gates"]["relative_improvement_gate_passes"]
        ),
        "default_sensitivity_pass_ratio_passes": bool(
            default_ratio >= float(args.min_sensitivity_pass_ratio)
        ),
        "stress_sensitivity_pass_ratio_passes": bool(
            stress_ratio >= float(args.min_sensitivity_pass_ratio)
        ),
        "default_metric_spread_passes": bool(
            all(value <= float(args.max_metric_spread) for value in default_spreads.values())
        ),
        "stress_metric_spread_passes": bool(
            all(value <= float(args.max_metric_spread) for value in stress_spreads.values())
        ),
        "default_cross_capital_passes": bool(cross_default >= int(args.min_cross_capital_passes)),
        "stress_cross_capital_passes": bool(cross_stress >= int(args.min_cross_capital_passes)),
        "production_minimum_passes": bool(
            base_candidate["gates"]["production_minimum_gate_passes"]
            and stress_candidate["gates"]["production_minimum_gate_passes"]
        ),
    }
    robustness = bool(all(gates.values()))
    blockers = [name for name, passes in gates.items() if not passes]
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V40-robustness-v1",
        "research_only": True,
        "production_ready": False,
        "robustness_gate_passes": robustness,
        "candidate": candidate_kwargs,
        "thresholds": {
            "min_cross_capital_passes": int(args.min_cross_capital_passes),
            "min_sensitivity_pass_ratio": float(args.min_sensitivity_pass_ratio),
            "max_metric_spread": float(args.max_metric_spread),
        },
        "candidate_metrics": {
            "default": base_candidate["full"],
            "cost_stress": stress_candidate["full"],
        },
        "sensitivity": {
            "default_pass_ratio": round(default_ratio, 6),
            "cost_stress_pass_ratio": round(stress_ratio, 6),
            "default_metric_spreads": {
                key: round(value, 6) for key, value in default_spreads.items()
            },
            "cost_stress_metric_spreads": {
                key: round(value, 6) for key, value in stress_spreads.items()
            },
        },
        "cross_capital": {
            "default_pass_count": cross_default,
            "default_passing_capitals": cross_default_capitals,
            "cost_stress_pass_count": cross_stress,
            "cost_stress_passing_capitals": cross_stress_capitals,
        },
        "gates": gates,
        "blockers": blockers,
        "source_files": {
            "base": str(Path(args.base)),
            "stress": str(Path(args.stress)),
            "sensitivity": str(Path(args.sensitivity)),
            "sensitivity_stress": str(Path(args.sensitivity_stress)),
        },
    }


def main() -> None:
    args = _parse_args()
    report = build_report(args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "production_ready": False,
                "robustness_gate_passes": report["robustness_gate_passes"],
                "blockers": report["blockers"],
                "output": str(output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
