#!/usr/bin/env python3
"""Plan a V29 sleeve/bucket-aware risk-flag hypothesis.

This consumes the May-2026 historical failed-bucket contrast plus regime timing
evidence and emits a fail-closed research plan. It does not change strategy code,
thresholds, factor signs or exposures; it only defines the next falsifiable
hypothesis and validation gates.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _paths import RESULTS_DIR

DEFAULT_CONTRAST = RESULTS_DIR / "quant_v29_recent90_historical_contrast_may2026.json"
DEFAULT_TIMING = RESULTS_DIR / "quant_v29_recent90_regime_timing_may2026.json"
DEFAULT_OUTPUT = RESULTS_DIR / "quant_v29_bucket_risk_flag_hypothesis.json"
DEFAULT_REPORT = Path("docs/research/quant_v29_bucket_risk_flag_hypothesis.md")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contrast-json", default=str(DEFAULT_CONTRAST))
    parser.add_argument("--timing-json", default=str(DEFAULT_TIMING))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--report-md", default=str(DEFAULT_REPORT))
    parser.add_argument("--top-n", type=int, default=8)
    return parser.parse_args()


def _safe_float(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def score_bucket_stress(bucket_type: str, item: dict[str, Any]) -> dict[str, Any]:
    """Score a failed bucket by target loss plus historical positive edge lost."""
    target = _safe_float(item.get("target_contribution"))
    hist_mean = _safe_float(item.get("historical_positive_mean"))
    hit_rate = _safe_float(item.get("historical_positive_hit_rate"))
    direction_flip = bool(item.get("direction_flip"))
    stress_score = abs(min(target, 0.0)) + max(hist_mean, 0.0) * max(hit_rate, 0.0)
    eligible = direction_flip and target < 0.0 and hist_mean > 0.0 and hit_rate >= 0.5
    return {
        "bucket_type": str(bucket_type),
        "bucket": str(item.get("bucket", "unknown")),
        "target_contribution": round(target, 6),
        "historical_positive_mean": round(hist_mean, 6),
        "historical_positive_hit_rate": round(hit_rate, 6),
        "direction_flip": direction_flip,
        "stress_score": round(stress_score, 6),
        "eligible_for_flag_hypothesis": bool(eligible),
        "source_diagnosis": item.get("diagnosis"),
    }


def rank_flag_inputs(
    contrasts: dict[str, list[dict[str, Any]]], *, limit: int = 8
) -> list[dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    for bucket_type, items in contrasts.items():
        for item in items or []:
            row = score_bucket_stress(bucket_type, item)
            if row["eligible_for_flag_hypothesis"]:
                scored.append(row)
    scored.sort(key=lambda row: row["stress_score"], reverse=True)
    return scored[:limit]


def _timing_by_candidate(timing_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in timing_payload.get("summaries", []):
        if isinstance(item, dict) and item.get("candidate"):
            out[str(item["candidate"])] = item
    return out


def build_candidate_hypothesis(
    contrast_candidate: dict[str, Any],
    timing_candidate: dict[str, Any] | None,
    *,
    top_n: int = 8,
) -> dict[str, Any]:
    candidate = str(contrast_candidate.get("candidate"))
    ranked = rank_flag_inputs(contrast_candidate.get("contrasts", {}), limit=top_n)
    timing_target = (timing_candidate or {}).get("target_month", {}) if timing_candidate else {}
    timing_missed = timing_target.get("diagnosis") == "risk_off_missed_loss_window"
    contrast_flip = contrast_candidate.get("diagnosis") == "failed_buckets_flip_vs_positive_history"
    hypothesis_ready = bool(ranked) and timing_missed and contrast_flip
    primary_inputs = ranked[: min(4, len(ranked))]
    return {
        "candidate": candidate,
        "production_ready": False,
        "not_parameter_tuning": True,
        "can_implement_strategy_change_now": False,
        "hypothesis_ready_for_simulation": bool(hypothesis_ready),
        "hypothesis_type": (
            "sleeve_bucket_stress_flag" if hypothesis_ready else "insufficient_evidence"
        ),
        "evidence_summary": {
            "contrast_diagnosis": contrast_candidate.get("diagnosis"),
            "timing_diagnosis": timing_target.get("diagnosis"),
            "risk_off_ratio": timing_target.get("risk_off_ratio"),
            "target_raw_contribution": contrast_candidate.get("target_raw_contribution"),
            "positive_month_count": contrast_candidate.get("positive_month_count"),
            "month_count": contrast_candidate.get("month_count"),
        },
        "ranked_flag_inputs": ranked,
        "proposed_flag_spec": {
            "name": f"{candidate}_bucket_stress_guard_hypothesis",
            "trigger_inputs": primary_inputs,
            "trigger_concept": (
                "During a benign market-level regime, monitor rolling raw sleeve/bucket contributions "
                "for historically positive buckets. If multiple high-stress buckets flip negative together, "
                "flag local sleeve/bucket stress before market-level mom/vol risk-off triggers."
            ),
            "allowed_research_actions": [
                "simulate exposure gating only inside research scripts",
                "compare against unchanged V29 baseline",
                "record all changes as fail-closed hypotheses",
            ],
            "forbidden_actions": [
                "no production config change",
                "no factor sign flip",
                "no top_n or threshold tuning in production code",
                "no crisis weight increase without full validation",
            ],
        },
        "required_validation": [
            "walk_forward_oos_vs_unchanged_v29_baseline",
            "randomized_bucket_flag_baseline",
            "look_ahead_bias_audit_for_bucket_contribution_inputs",
            "monthly_distribution_check_including_2018_2020_2022_2026",
            "recent90_retest_and_worst_month_attribution",
            "paper_evidence_unchanged_until_real_daily_reports_accumulate",
        ],
        "next_step": (
            "Implement a research-only simulation of this flag against unchanged V29 baseline. "
            "Do not touch production strategy or feature passes."
            if hypothesis_ready
            else "Collect more attribution evidence before simulation."
        ),
    }


def build_full_report(
    contrast_payload: dict[str, Any],
    timing_payload: dict[str, Any],
    generated_at: datetime,
    *,
    top_n: int,
) -> dict[str, Any]:
    timing_map = _timing_by_candidate(timing_payload)
    hypotheses = [
        build_candidate_hypothesis(item, timing_map.get(str(item.get("candidate"))), top_n=top_n)
        for item in contrast_payload.get("candidates", [])
    ]
    return {
        "ts": generated_at.isoformat(),
        "version": "V29-bucket-risk-flag-hypothesis-plan",
        "research_only": True,
        "production_ready": False,
        "not_parameter_tuning": True,
        "source_reports": {
            "historical_contrast_version": contrast_payload.get("version"),
            "regime_timing_version": timing_payload.get("version"),
            "target_month": contrast_payload.get("target_month"),
        },
        "hypotheses": hypotheses,
        "global_decision": {
            "can_change_strategy_now": False,
            "can_simulate_research_hypothesis_next": any(
                item["hypothesis_ready_for_simulation"] for item in hypotheses
            ),
            "reason": "Cross-section, timing and historical contrast now support a falsifiable local bucket-stress flag hypothesis, but no production change is validated.",
        },
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# V29 bucket/sleeve-aware risk flag hypothesis plan",
        "",
        "状态：研究假设计划；不调参；不改策略；不解除 production blocker。",
        "",
        f"- target_month: {report['source_reports'].get('target_month')}",
        f"- production_ready: {str(report['production_ready']).lower()}",
        f"- can_simulate_research_hypothesis_next: {str(report['global_decision']['can_simulate_research_hypothesis_next']).lower()}",
        "",
    ]
    for item in report["hypotheses"]:
        top = item["ranked_flag_inputs"][:5]
        lines.extend(
            [
                f"## {item['candidate']}",
                "",
                f"- hypothesis_type: {item['hypothesis_type']}",
                f"- hypothesis_ready_for_simulation: {str(item['hypothesis_ready_for_simulation']).lower()}",
                f"- can_implement_strategy_change_now: {str(item['can_implement_strategy_change_now']).lower()}",
                f"- timing_diagnosis: {item['evidence_summary'].get('timing_diagnosis')}",
                f"- contrast_diagnosis: {item['evidence_summary'].get('contrast_diagnosis')}",
                "- top stress inputs: "
                + ", ".join(
                    f"{row['bucket_type']}:{row['bucket']} score={row['stress_score']}"
                    for row in top
                ),
                "",
            ]
        )
    lines.extend(
        [
            "## Required validation before any strategy change",
            "",
            "- walk_forward_oos_vs_unchanged_v29_baseline",
            "- randomized_bucket_flag_baseline",
            "- look_ahead_bias_audit_for_bucket_contribution_inputs",
            "- monthly_distribution_check_including_2018_2020_2022_2026",
            "- recent90_retest_and_worst_month_attribution",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    contrast = json.loads(Path(args.contrast_json).read_text(encoding="utf-8"))
    timing = json.loads(Path(args.timing_json).read_text(encoding="utf-8"))
    report = build_full_report(contrast, timing, datetime.now(timezone.utc), top_n=int(args.top_n))
    _write_json(Path(args.output_json), report)
    _write_markdown(Path(args.report_md), report)
    print(
        json.dumps(
            {
                "production_ready": False,
                "not_parameter_tuning": True,
                "output": args.output_json,
                "report": args.report_md,
                "can_simulate_research_hypothesis_next": report["global_decision"][
                    "can_simulate_research_hypothesis_next"
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
