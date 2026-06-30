#!/usr/bin/env python3
"""Compare true V29 daily bucket-aware flag against randomized baselines.

Research-only falsification check. It preserves the observed count of lagged
liquidity/score bucket-aware flagged days and their trigger metadata, randomizes
placement across eligible non-risk-off target-month days, and compares the
observed attribution delta against random timing. It does not change strategy
code, production config, feature passes, or risk gates.
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
from simulate_v29_daily_bucket_aware_flag import (
    simulate_bucket_aware_gate,
    summarize_bucket_aware_simulation,
)

DEFAULT_SIMULATION = RESULTS_DIR / "quant_v29_daily_bucket_aware_flag_simulation.json"
DEFAULT_OUTPUT = RESULTS_DIR / "quant_v29_daily_bucket_aware_random_baseline.json"
DEFAULT_REPORT = Path("docs/research/quant_v29_daily_bucket_aware_random_baseline.md")
DEFAULT_TRIALS = 500
DEFAULT_SEED = 20260630


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


def _observed_flag_templates(rows: pd.DataFrame) -> list[dict[str, Any]]:
    if "bucket_aware_flag" not in rows:
        return []
    templates: list[dict[str, Any]] = []
    flagged = rows[rows["bucket_aware_flag"].astype(bool)]
    for row in flagged.itertuples(index=False):
        triggers = getattr(row, "triggered_buckets", [])
        if not isinstance(triggers, list):
            triggers = []
        templates.append(
            {
                "triggered_bucket_count": int(
                    getattr(row, "triggered_bucket_count", len(triggers)) or 0
                ),
                "triggered_buckets": [str(item) for item in triggers],
                "flagged_negative_bucket_contribution": _safe_float(
                    getattr(row, "flagged_negative_bucket_contribution", 0.0)
                ),
            }
        )
    return templates


def randomized_bucket_aware_schedule(rows: pd.DataFrame, *, seed: int) -> pd.DataFrame:
    """Randomize bucket-aware flag dates while preserving observed trigger metadata."""
    rng = random.Random(int(seed))
    out = rows.copy().reset_index(drop=True)
    templates = _observed_flag_templates(out)
    flag_count = len(templates)
    eligible = out.index[
        ~out.get("risk_off", pd.Series(False, index=out.index)).astype(bool)
    ].tolist()
    flag_count = min(flag_count, len(eligible))
    selected = set(rng.sample(eligible, flag_count)) if flag_count else set()
    shuffled_templates = list(templates)
    rng.shuffle(shuffled_templates)

    flags: list[bool] = []
    counts: list[int] = []
    triggers_out: list[list[str]] = []
    neg_contrib: list[float] = []
    assigned = 0
    for idx in out.index:
        if idx in selected:
            template = shuffled_templates[assigned % len(shuffled_templates)]
            flags.append(True)
            counts.append(int(template["triggered_bucket_count"]))
            triggers_out.append(list(template["triggered_buckets"]))
            neg_contrib.append(_safe_float(template["flagged_negative_bucket_contribution"]))
            assigned += 1
        else:
            flags.append(False)
            counts.append(0)
            triggers_out.append([])
            neg_contrib.append(0.0)
    out["bucket_aware_flag"] = pd.Series(flags, dtype=object)
    out["triggered_bucket_count"] = counts
    out["triggered_buckets"] = triggers_out
    out["flagged_negative_bucket_contribution"] = neg_contrib
    return out


def run_random_baseline_trials(
    rows: pd.DataFrame,
    *,
    n_trials: int,
    seed: int,
    reduction_fraction: float,
    max_daily_delta_fraction: float,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for trial_idx in range(int(n_trials)):
        randomized = randomized_bucket_aware_schedule(rows, seed=int(seed) + trial_idx)
        flags = randomized[
            [
                "candidate",
                "date",
                "bucket_aware_flag",
                "triggered_bucket_count",
                "triggered_buckets",
                "flagged_negative_bucket_contribution",
            ]
        ].copy()
        portfolio_rows = rows.drop(
            columns=[
                "bucket_aware_flag",
                "triggered_bucket_count",
                "triggered_buckets",
                "flagged_negative_bucket_contribution",
                "simulated_return",
                "bucket_aware_delta",
            ],
            errors="ignore",
        )
        simulated = simulate_bucket_aware_gate(
            portfolio_rows,
            flags,
            reduction_fraction=float(reduction_fraction),
            max_daily_delta_fraction=float(max_daily_delta_fraction),
        )
        candidate_name = (
            str(rows["candidate"].iloc[0])
            if "candidate" in rows and not rows.empty
            else f"random_{trial_idx}"
        )
        summary = summarize_bucket_aware_simulation(candidate_name, simulated)
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
            "look_ahead_bias_audit_for_lagged_bucket_state",
            "monthly_distribution_check_including_2018_2020_2022_2026",
            "walk_forward_oos_vs_unchanged_v29_baseline",
            "do_not_promote_bucket_aware_flag_without_full_validation",
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
    params = simulation_payload.get("simulation_parameters", {})
    reduction = _safe_float(params.get("reduction_fraction"))
    cap_fraction = _safe_float(params.get("max_daily_delta_fraction"))
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
            max_daily_delta_fraction=cap_fraction,
        )
        summaries.append(summarize_random_baseline(candidate, observed_summary, candidate_trials))
        trial_samples[candidate] = candidate_trials[:20]
    return {
        "ts": generated_at.isoformat(),
        "version": "V29-daily-bucket-aware-random-baseline",
        "research_only": True,
        "production_ready": False,
        "not_parameter_tuning": True,
        "target_month": simulation_payload.get("target_month"),
        "parameters": {
            "trials": int(trials),
            "seed": int(seed),
            "reduction_fraction": reduction,
            "max_daily_delta_fraction": cap_fraction,
            "randomizes_only_flag_schedule": True,
            "preserves_trigger_metadata": True,
        },
        "summaries": summaries,
        "trial_samples_first20": trial_samples,
        "global_decision": {
            "can_change_strategy_now": False,
            "can_claim_production_ready": False,
            "next_required_step": "look_ahead_monthly_walk_forward_validation",
            "reason": "Bucket-aware flag timing must beat random and still pass look-ahead/monthly/WF validation before strategy code can be considered.",
        },
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# V29 daily bucket-aware flag randomized baseline",
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
            "这只是 bucket-aware randomized baseline。即使 observed timing 优于随机，也仍然不能进策略代码；下一步必须做 look-ahead、monthly、WF 验证。",
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
                "target_month": report["target_month"],
                "output": args.output_json,
                "report": args.report_md,
                "summaries": report["summaries"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
