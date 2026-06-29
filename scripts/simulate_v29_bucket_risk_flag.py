#!/usr/bin/env python3
"""Research-only simulation for the V29 bucket/sleeve stress flag hypothesis.

This script consumes the fail-closed hypothesis plan plus V29 regime timing daily
records and runs a lagged attribution simulation. It does NOT modify strategy
code, factor signs, thresholds, crisis weights, feature_list passes, or production
configuration.

Important limitation: this is not a production portfolio engine. It approximates
what would have happened if a lagged sleeve-stress flag reduced only the flagged
sleeve contribution in the research attribution trace. Any favorable result must
still pass WF/random/look-ahead/monthly validation before becoming a strategy
change.
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

DEFAULT_HYPOTHESIS = RESULTS_DIR / "quant_v29_bucket_risk_flag_hypothesis.json"
DEFAULT_TIMING = RESULTS_DIR / "quant_v29_recent90_regime_timing_may2026.json"
DEFAULT_OUTPUT = RESULTS_DIR / "quant_v29_bucket_risk_flag_simulation.json"
DEFAULT_REPORT = Path("docs/research/quant_v29_bucket_risk_flag_simulation.md")
DEFAULT_REDUCTION_FRACTION = 0.5
DEFAULT_OBSERVATION_DAYS = 3


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hypothesis-json", default=str(DEFAULT_HYPOTHESIS))
    parser.add_argument("--timing-json", default=str(DEFAULT_TIMING))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--report-md", default=str(DEFAULT_REPORT))
    parser.add_argument("--reduction-fraction", type=float, default=DEFAULT_REDUCTION_FRACTION)
    parser.add_argument("--observation-days", type=int, default=DEFAULT_OBSERVATION_DAYS)
    return parser.parse_args()


def _safe_float(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def _compound(values: pd.Series) -> float:
    total = 1.0
    for value in values:
        total *= 1.0 + _safe_float(value)
    return total - 1.0


def _sleeve_contrib_column(sleeve_name: str) -> str:
    return f"contrib_{sleeve_name}"


def build_sleeve_trigger_inputs(
    hypothesis: dict[str, Any],
    *,
    target_day_count: int,
    observation_days: int,
    daily_rows: pd.DataFrame | None = None,
    target_month: str | None = None,
) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    denom = max(int(target_day_count), 1)
    obs = max(int(observation_days), 1)
    for item in hypothesis.get("ranked_flag_inputs", []):
        if item.get("bucket_type") != "sleeve":
            continue
        target = _safe_float(item.get("target_contribution"))
        basis = "raw_historical_contrast"
        scaled_target = target
        if daily_rows is not None and target_month and not daily_rows.empty:
            column = _sleeve_contrib_column(str(item.get("bucket")))
            if column in daily_rows.columns and "month" in daily_rows.columns:
                target_slice = daily_rows[daily_rows["month"] == target_month]
                trace_sum = float(
                    pd.to_numeric(target_slice[column], errors="coerce").fillna(0.0).sum()
                )
                if trace_sum < 0.0:
                    scaled_target = trace_sum
                    basis = "scaled_daily_meta_trace"
        threshold = -abs(scaled_target) / denom * obs
        inputs.append(
            {
                "bucket_type": "sleeve",
                "bucket": str(item.get("bucket")),
                "stress_score": item.get("stress_score"),
                "target_contribution": round(target, 6),
                "scaled_trace_target_contribution": round(scaled_target, 6),
                "threshold_basis": basis,
                "daily_trigger_threshold": round(threshold, 8),
            }
        )
    return inputs


def apply_lagged_bucket_stress_flags(
    rows: pd.DataFrame, trigger_inputs: list[dict[str, Any]], *, observation_days: int
) -> pd.DataFrame:
    """Flag local stress using only prior rows' sleeve contribution sums."""
    out = rows.copy().reset_index(drop=True)
    out["bucket_stress_flag"] = False
    out["triggered_buckets"] = [[] for _ in range(len(out))]
    if out.empty or not trigger_inputs:
        out["bucket_stress_flag"] = out["bucket_stress_flag"].astype(object)
        return out
    obs = max(int(observation_days), 1)
    triggered_lists: list[list[str]] = []
    flags: list[bool] = []
    for idx, row in out.iterrows():
        triggered: list[str] = []
        if bool(row.get("risk_off", False)):
            triggered_lists.append(triggered)
            flags.append(False)
            continue
        start = max(0, idx - obs)
        history = out.iloc[start:idx]
        for item in trigger_inputs:
            bucket = str(item.get("bucket"))
            column = _sleeve_contrib_column(bucket)
            if column not in history.columns or history.empty:
                continue
            rolling_sum = float(pd.to_numeric(history[column], errors="coerce").fillna(0.0).sum())
            threshold = _safe_float(item.get("daily_trigger_threshold"))
            if rolling_sum <= threshold:
                triggered.append(f"sleeve:{bucket}")
        triggered_lists.append(triggered)
        flags.append(bool(triggered))
    out["bucket_stress_flag"] = pd.Series(flags, dtype=object)
    out["triggered_buckets"] = triggered_lists
    return out


def _triggered_sleeve_columns(triggered_buckets: Any) -> list[str]:
    if not isinstance(triggered_buckets, list):
        return []
    columns: list[str] = []
    for item in triggered_buckets:
        text = str(item)
        if not text.startswith("sleeve:"):
            continue
        columns.append(_sleeve_contrib_column(text.split(":", 1)[1]))
    return columns


def simulate_research_gate(rows: pd.DataFrame, *, reduction_fraction: float) -> pd.DataFrame:
    """Reduce flagged sleeve contribution in a research attribution trace."""
    out = rows.copy().reset_index(drop=True)
    reduction = min(max(float(reduction_fraction), 0.0), 1.0)
    simulated_returns: list[float] = []
    deltas: list[float] = []
    for _, row in out.iterrows():
        base_return = _safe_float(row.get("return"))
        if not bool(row.get("bucket_stress_flag", False)):
            simulated_returns.append(base_return)
            deltas.append(0.0)
            continue
        current_contrib = 0.0
        for column in _triggered_sleeve_columns(row.get("triggered_buckets")):
            current_contrib += _safe_float(row.get(column))
        delta = -reduction * current_contrib
        simulated_returns.append(round(base_return + delta, 12))
        deltas.append(round(delta, 12))
    out["simulated_return"] = simulated_returns
    out["avoided_loss_contribution"] = deltas
    return out


def summarize_candidate_simulation(candidate: str, rows: pd.DataFrame) -> dict[str, Any]:
    if rows.empty:
        return {
            "candidate": candidate,
            "production_ready": False,
            "not_parameter_tuning": True,
            "can_change_strategy_now": False,
            "diagnosis": "no_rows",
        }
    work = rows.copy()
    baseline_returns = pd.to_numeric(work["return"], errors="coerce").fillna(0.0)
    simulated_returns = pd.to_numeric(work["simulated_return"], errors="coerce").fillna(0.0)
    if "avoided_loss_contribution" not in work.columns:
        work["avoided_loss_contribution"] = simulated_returns - baseline_returns
    if "triggered_buckets" not in work.columns:
        work["triggered_buckets"] = [[] for _ in range(len(work))]
    baseline = _compound(baseline_returns)
    simulated = _compound(simulated_returns)
    flags = (
        work["bucket_stress_flag"].astype(bool) if "bucket_stress_flag" in work else pd.Series([])
    )
    flagged = work[flags].copy() if len(flags) else pd.DataFrame()
    return {
        "candidate": candidate,
        "production_ready": False,
        "not_parameter_tuning": True,
        "can_change_strategy_now": False,
        "baseline_compound_return": round(float(baseline), 6),
        "simulated_compound_return": round(float(simulated), 6),
        "simulated_minus_baseline": round(float(simulated - baseline), 6),
        "flagged_day_count": int(len(flagged)),
        "n_days": int(len(work)),
        "flagged_day_ratio": round(float(len(flagged) / max(len(work), 1)), 6),
        "net_attribution_delta": round(
            float(work.get("avoided_loss_contribution", pd.Series([0.0])).sum()), 6
        ),
        "worst_flagged_days": [
            {
                "date": str(row.date),
                "baseline_return": round(_safe_float(getattr(row, "return_", 0.0)), 6),
                "simulated_return": round(_safe_float(row.simulated_return), 6),
                "delta": round(_safe_float(row.avoided_loss_contribution), 6),
                "triggered_buckets": list(row.triggered_buckets),
            }
            for row in flagged.rename(columns={"return": "return_"})
            .sort_values("return_")
            .head(8)
            .itertuples(index=False)
        ],
        "required_validation_before_change": [
            "walk_forward_oos_vs_unchanged_v29_baseline",
            "randomized_bucket_flag_baseline",
            "look_ahead_bias_audit_for_lagged_inputs",
            "monthly_distribution_check_including_2018_2020_2022_2026",
            "recent90_retest_and_worst_month_attribution",
        ],
    }


def build_full_report(
    hypothesis_payload: dict[str, Any],
    timing_payload: dict[str, Any],
    generated_at: datetime,
    *,
    reduction_fraction: float,
    observation_days: int,
) -> dict[str, Any]:
    daily = pd.DataFrame(timing_payload.get("daily_records", []))
    target_month = str(
        timing_payload.get("target_month")
        or hypothesis_payload.get("source_reports", {}).get("target_month")
    )
    if not daily.empty:
        daily["date"] = pd.to_datetime(daily["date"], errors="coerce")
        daily = daily.dropna(subset=["date"])
        daily["month"] = daily["date"].dt.to_period("M").astype(str)
    target_day_count = (
        int(daily[daily["month"] == target_month]["date"].nunique()) if not daily.empty else 0
    )
    summaries: list[dict[str, Any]] = []
    trigger_specs: dict[str, list[dict[str, Any]]] = {}
    simulated_records: list[dict[str, Any]] = []
    for hypothesis in hypothesis_payload.get("hypotheses", []):
        candidate = str(hypothesis.get("candidate"))
        candidate_rows = (
            daily[daily["candidate"] == candidate].copy() if not daily.empty else pd.DataFrame()
        )
        trigger_inputs = build_sleeve_trigger_inputs(
            hypothesis,
            target_day_count=target_day_count,
            observation_days=observation_days,
            daily_rows=candidate_rows,
            target_month=target_month,
        )
        trigger_specs[candidate] = trigger_inputs
        flagged = apply_lagged_bucket_stress_flags(
            candidate_rows, trigger_inputs, observation_days=observation_days
        )
        simulated = simulate_research_gate(flagged, reduction_fraction=reduction_fraction)
        target_only = (
            simulated[simulated["month"] == target_month].copy()
            if "month" in simulated
            else simulated
        )
        summaries.append(summarize_candidate_simulation(candidate, target_only))
        simulated_records.extend(target_only.to_dict(orient="records"))
    return {
        "ts": generated_at.isoformat(),
        "version": "V29-bucket-risk-flag-research-simulation",
        "research_only": True,
        "production_ready": False,
        "not_parameter_tuning": True,
        "target_month": target_month,
        "simulation_parameters": {
            "observation_days": int(observation_days),
            "reduction_fraction": float(reduction_fraction),
            "target_day_count": int(target_day_count),
            "lagged_inputs_only": True,
        },
        "trigger_specs": trigger_specs,
        "summaries": summaries,
        "simulated_daily_records": simulated_records,
        "global_decision": {
            "can_change_strategy_now": False,
            "can_claim_production_ready": False,
            "can_run_validation_next": True,
            "reason": "Attribution-only simulation is a falsification step; favorable May results are insufficient without WF/random/look-ahead/monthly validation.",
        },
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# V29 bucket/sleeve stress flag research simulation",
        "",
        "状态：研究仿真；不调参；不改策略；不解除 production blocker。",
        "",
        f"- target_month: {report['target_month']}",
        f"- production_ready: {str(report['production_ready']).lower()}",
        f"- observation_days: {report['simulation_parameters']['observation_days']}",
        f"- reduction_fraction: {report['simulation_parameters']['reduction_fraction']}",
        f"- lagged_inputs_only: {str(report['simulation_parameters']['lagged_inputs_only']).lower()}",
        "",
    ]
    for item in report["summaries"]:
        lines.extend(
            [
                f"## {item['candidate']}",
                "",
                f"- baseline_compound_return: {item.get('baseline_compound_return')}",
                f"- simulated_compound_return: {item.get('simulated_compound_return')}",
                f"- simulated_minus_baseline: {item.get('simulated_minus_baseline')}",
                f"- flagged_day_count: {item.get('flagged_day_count')} / {item.get('n_days')}",
                f"- net_attribution_delta: {item.get('net_attribution_delta')}",
                "",
            ]
        )
    lines.extend(
        [
            "## Decision",
            "",
            "这只是 attribution-level lagged simulation。即使改善 May-2026，也不能直接改生产策略；下一步必须跑 WF、randomized flag baseline、look-ahead audit、monthly distribution checks。",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    hypothesis = json.loads(Path(args.hypothesis_json).read_text(encoding="utf-8"))
    timing = json.loads(Path(args.timing_json).read_text(encoding="utf-8"))
    report = build_full_report(
        hypothesis,
        timing,
        datetime.now(timezone.utc),
        reduction_fraction=float(args.reduction_fraction),
        observation_days=int(args.observation_days),
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
                "summaries": [
                    {
                        "candidate": item["candidate"],
                        "baseline": item.get("baseline_compound_return"),
                        "simulated": item.get("simulated_compound_return"),
                        "delta": item.get("simulated_minus_baseline"),
                        "flagged_days": item.get("flagged_day_count"),
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
