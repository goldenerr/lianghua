#!/usr/bin/env python3
"""Compare V29 bucket stress flag against randomized flag baselines.

This is a research-only falsification check. It preserves the observed number of
flagged days and triggered sleeve labels, randomizes their placement across
non-risk-off target-month days, and compares attribution deltas against the
observed lagged sleeve-only flag. It does not change strategy code or production
configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

import pandas as pd
from _paths import RESULTS_DIR
from simulate_v29_bucket_risk_flag import simulate_research_gate, summarize_candidate_simulation

DEFAULT_SIMULATION = RESULTS_DIR / "quant_v29_bucket_risk_flag_simulation.json"
DEFAULT_OUTPUT = RESULTS_DIR / "quant_v29_bucket_risk_flag_random_baseline.json"
DEFAULT_REPORT = Path("docs/research/quant_v29_bucket_risk_flag_random_baseline.md")
DEFAULT_TRIALS = 500
DEFAULT_SEED = 20260629


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulation-json", default=str(DEFAULT_SIMULATION))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--report-md", default=str(DEFAULT_REPORT))
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    return parser.parse_args()


def _safe_float(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def _observed_triggered_lists(rows: pd.DataFrame) -> list[list[str]]:
    out: list[list[str]] = []
    if "bucket_stress_flag" not in rows or "triggered_buckets" not in rows:
        return out
    for item in rows[rows["bucket_stress_flag"].astype(bool)]["triggered_buckets"].tolist():
        if isinstance(item, list) and item:
            out.append([str(x) for x in item])
    return out


def randomized_flag_schedule(rows: pd.DataFrame, *, seed: int) -> pd.DataFrame:
    """Randomize flag dates while preserving flag count and trigger labels."""
    rng = random.Random(int(seed))
    out = rows.copy().reset_index(drop=True)
    observed_flag_count = int(
        out.get("bucket_stress_flag", pd.Series(dtype=bool)).astype(bool).sum()
    )
    eligible = out.index[
        ~out.get("risk_off", pd.Series(False, index=out.index)).astype(bool)
    ].tolist()
    flag_count = min(observed_flag_count, len(eligible))
    triggered_templates = _observed_triggered_lists(out)
    if not triggered_templates:
        triggered_templates = [[]]
    selected = set(rng.sample(eligible, flag_count)) if flag_count else set()
    randomized_flags: list[bool] = []
    randomized_triggers: list[list[str]] = []
    assigned = 0
    for idx in out.index:
        if idx in selected:
            randomized_flags.append(True)
            randomized_triggers.append(
                list(triggered_templates[assigned % len(triggered_templates)])
            )
            assigned += 1
        else:
            randomized_flags.append(False)
            randomized_triggers.append([])
    out["bucket_stress_flag"] = pd.Series(randomized_flags, dtype=object)
    out["triggered_buckets"] = randomized_triggers
    return out


def run_random_baseline_trials(
    rows: pd.DataFrame, *, n_trials: int, seed: int, reduction_fraction: float
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for trial_idx in range(int(n_trials)):
        randomized = randomized_flag_schedule(rows, seed=int(seed) + trial_idx)
        simulated = simulate_research_gate(randomized, reduction_fraction=float(reduction_fraction))
        summary = summarize_candidate_simulation(f"random_{trial_idx}", simulated)
        results.append(
            {
                "trial": trial_idx,
                "simulated_minus_baseline": summary["simulated_minus_baseline"],
                "simulated_compound_return": summary["simulated_compound_return"],
                "flagged_day_count": summary["flagged_day_count"],
                "net_attribution_delta": summary["net_attribution_delta"],
            }
        )
    return results


def summarize_random_baseline(
    candidate: str, observed_summary: dict[str, Any], trials: list[dict[str, Any]]
) -> dict[str, Any]:
    values = [_safe_float(item.get("simulated_minus_baseline")) for item in trials]
    observed_delta = _safe_float(observed_summary.get("simulated_minus_baseline"))
    random_median = float(median(values)) if values else 0.0
    random_mean = float(mean(values)) if values else 0.0
    random_best = max(values) if values else 0.0
    random_worst = min(values) if values else 0.0
    percentile = (
        float(sum(1 for value in values if value <= observed_delta) / len(values))
        if values
        else 0.0
    )
    beats_random_median = observed_delta > random_median
    if observed_delta <= 0.0:
        diagnosis = "no_positive_observed_edge"
    elif not beats_random_median:
        diagnosis = "does_not_beat_random_median"
    else:
        diagnosis = "beats_random_median_only_not_validated"
    return {
        "candidate": candidate,
        "production_ready": False,
        "not_parameter_tuning": True,
        "can_change_strategy_now": False,
        "observed_delta": round(observed_delta, 6),
        "observed_flagged_day_count": int(observed_summary.get("flagged_day_count", 0)),
        "random_trial_count": int(len(values)),
        "random_median_delta": round(random_median, 6),
        "random_mean_delta": round(random_mean, 6),
        "random_best_delta": round(random_best, 6),
        "random_worst_delta": round(random_worst, 6),
        "observed_percentile_vs_random": round(percentile, 6),
        "beats_random_median": bool(beats_random_median),
        "diagnosis": diagnosis,
        "required_next_validation": [
            "look_ahead_bias_audit_for_lagged_inputs",
            "monthly_distribution_check_including_2018_2020_2022_2026",
            "do_not_promote_sleeve_only_flag_if_random_baseline_is_comparable_or_better",
        ],
    }


def _observed_summary_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item.get("candidate")): item for item in payload.get("summaries", [])}


def _stable_seed_offset(text: str) -> int:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100_000


def build_full_report(
    simulation_payload: dict[str, Any], generated_at: datetime, *, trials: int, seed: int
) -> dict[str, Any]:
    records = pd.DataFrame(simulation_payload.get("simulated_daily_records", []))
    observed = _observed_summary_map(simulation_payload)
    reduction = _safe_float(
        simulation_payload.get("simulation_parameters", {}).get("reduction_fraction")
    )
    summaries: list[dict[str, Any]] = []
    trial_samples: dict[str, list[dict[str, Any]]] = {}
    for candidate, observed_summary in observed.items():
        candidate_rows = (
            records[records["candidate"] == candidate].copy()
            if not records.empty
            else pd.DataFrame()
        )
        candidate_trials = run_random_baseline_trials(
            candidate_rows,
            n_trials=int(trials),
            seed=int(seed) + _stable_seed_offset(candidate),
            reduction_fraction=reduction,
        )
        summaries.append(summarize_random_baseline(candidate, observed_summary, candidate_trials))
        trial_samples[candidate] = candidate_trials[:20]
    return {
        "ts": generated_at.isoformat(),
        "version": "V29-bucket-risk-flag-random-baseline",
        "research_only": True,
        "production_ready": False,
        "not_parameter_tuning": True,
        "target_month": simulation_payload.get("target_month"),
        "parameters": {"trials": int(trials), "seed": int(seed), "reduction_fraction": reduction},
        "summaries": summaries,
        "trial_samples_first20": trial_samples,
        "global_decision": {
            "can_change_strategy_now": False,
            "can_claim_production_ready": False,
            "reason": "Randomized flag placement is a falsification baseline; a sleeve-only flag must beat this before any broader validation.",
        },
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# V29 bucket stress flag randomized baseline",
        "",
        "状态：研究对照；不调参；不改策略；不解除 production blocker。",
        "",
        f"- target_month: {report['target_month']}",
        f"- trials: {report['parameters']['trials']}",
        f"- seed: {report['parameters']['seed']}",
        f"- production_ready: {str(report['production_ready']).lower()}",
        "",
    ]
    for item in report["summaries"]:
        lines.extend(
            [
                f"## {item['candidate']}",
                "",
                f"- diagnosis: {item['diagnosis']}",
                f"- observed_delta: {item['observed_delta']}",
                f"- random_median_delta: {item['random_median_delta']}",
                f"- random_mean_delta: {item['random_mean_delta']}",
                f"- random_best_delta: {item['random_best_delta']}",
                f"- observed_percentile_vs_random: {item['observed_percentile_vs_random']}",
                f"- beats_random_median: {str(item['beats_random_median']).lower()}",
                "",
            ]
        )
    lines.extend(
        [
            "## Decision",
            "",
            "这只是 randomized baseline。若 observed flag 打不过随机或优势极弱，则 sleeve-only flag 应继续视为被证伪，不能进入策略代码。",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    payload = json.loads(Path(args.simulation_json).read_text(encoding="utf-8"))
    report = build_full_report(
        payload, datetime.now(timezone.utc), trials=int(args.trials), seed=int(args.seed)
    )
    _write_json(Path(args.output_json), report)
    _write_markdown(Path(args.report_md), report)
    print(
        json.dumps(
            {
                "production_ready": False,
                "not_parameter_tuning": True,
                "output": args.output_json,
                "report": args.report_md,
                "summaries": [
                    {
                        "candidate": item["candidate"],
                        "observed_delta": item["observed_delta"],
                        "random_median_delta": item["random_median_delta"],
                        "diagnosis": item["diagnosis"],
                    }
                    for item in report["summaries"]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
