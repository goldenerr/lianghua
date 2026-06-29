#!/usr/bin/env python3
"""Simulate a true daily liquidity/score bucket-aware V29 stress flag.

This research-only script follows the daily bucket-state diagnostic. Unlike the
falsified sleeve-only proxy, it uses lagged liquidity and score bucket stress
state built from selected-stock rows. It remains an attribution-level simulation:
no strategy code, parameters, production config, feature passes, or risk gates are
changed.
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
from diagnose_v29_recent90_daily_bucket_state import (
    _build_bucket_trigger_specs,
    _trace_rows,
    add_lagged_stress_state,
    aggregate_daily_bucket_state,
)

DEFAULT_HYPOTHESIS = RESULTS_DIR / "quant_v29_bucket_risk_flag_hypothesis.json"
DEFAULT_TIMING = RESULTS_DIR / "quant_v29_recent90_regime_timing_may2026.json"
DEFAULT_OUTPUT = RESULTS_DIR / "quant_v29_daily_bucket_aware_flag_simulation.json"
DEFAULT_REPORT = Path("docs/research/quant_v29_daily_bucket_aware_flag_simulation.md")
DEFAULT_CANDIDATES = "v29_price_meta_longhorizon_guard,v29_price_meta_ultradefensive"
DEFAULT_REDUCTION_FRACTION = 0.5
DEFAULT_MAX_DAILY_DELTA_FRACTION = 0.25


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", default=DEFAULT_CANDIDATES)
    parser.add_argument("--universe", default="data/stock_list.json")
    parser.add_argument("--start-date", default="20060101")
    parser.add_argument("--end-date", default="20260625")
    parser.add_argument("--target-month", default="2026-05")
    parser.add_argument("--pre-days", type=int, default=20)
    parser.add_argument("--min-history", type=int, default=252)
    parser.add_argument("--hypothesis-json", default=str(DEFAULT_HYPOTHESIS))
    parser.add_argument("--timing-json", default=str(DEFAULT_TIMING))
    parser.add_argument("--observation-days", type=int, default=3)
    parser.add_argument("--reduction-fraction", type=float, default=DEFAULT_REDUCTION_FRACTION)
    parser.add_argument(
        "--max-daily-delta-fraction", type=float, default=DEFAULT_MAX_DAILY_DELTA_FRACTION
    )
    parser.add_argument(
        "--trading-status-path",
        default="data/security_master/free_pit_approx/trading_status_v31.parquet",
    )
    parser.add_argument("--require-trading-status", action="store_true", default=True)
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
    for value in values:
        total *= 1.0 + _safe_float(value)
    return total - 1.0


def build_date_level_bucket_flags(flagged_state: pd.DataFrame) -> pd.DataFrame:
    """Collapse flagged liquidity/score bucket-days to candidate/date flags.

    Sleeve rows are intentionally excluded here because the sleeve-only proxy was
    already falsified; this simulation tests the missing liquidity/score state.
    """
    columns = [
        "candidate",
        "date",
        "bucket_aware_flag",
        "triggered_bucket_count",
        "triggered_buckets",
        "flagged_negative_bucket_contribution",
    ]
    if flagged_state.empty:
        return pd.DataFrame(columns=columns)
    work = flagged_state.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    work = work[
        work.get("bucket_stress_flag", pd.Series(False, index=work.index)).astype(bool)
        & work["bucket_type"].isin(["liquidity_bucket", "score_bucket"])
    ].copy()
    if work.empty:
        return pd.DataFrame(columns=columns)
    rows: list[dict[str, Any]] = []
    for (candidate, date), group in work.groupby(["candidate", "date"]):
        negative_sum = float(
            pd.to_numeric(group["contribution_sum"], errors="coerce").clip(upper=0.0).sum()
        )
        triggered = [f"{row.bucket_type}:{row.bucket}" for row in group.itertuples(index=False)]
        rows.append(
            {
                "candidate": str(candidate),
                "date": str(date),
                "bucket_aware_flag": True,
                "triggered_bucket_count": int(len(triggered)),
                "triggered_buckets": triggered,
                "flagged_negative_bucket_contribution": round(negative_sum, 8),
            }
        )
    out = pd.DataFrame(rows, columns=columns)
    out["bucket_aware_flag"] = out["bucket_aware_flag"].astype(object)
    return out


def simulate_bucket_aware_gate(
    portfolio_rows: pd.DataFrame,
    date_flags: pd.DataFrame,
    *,
    reduction_fraction: float,
    max_daily_delta_fraction: float,
) -> pd.DataFrame:
    """Apply a capped attribution delta on flagged negative portfolio days."""
    left = portfolio_rows.copy()
    left["date"] = pd.to_datetime(left["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    flags = date_flags.copy()
    if flags.empty:
        flags = pd.DataFrame(
            columns=[
                "candidate",
                "date",
                "bucket_aware_flag",
                "triggered_bucket_count",
                "triggered_buckets",
                "flagged_negative_bucket_contribution",
            ]
        )
    merged = left.merge(flags, on=["candidate", "date"], how="left", validate="many_to_one")
    if "bucket_aware_flag" not in merged.columns:
        merged["bucket_aware_flag"] = False
    merged["bucket_aware_flag"] = merged["bucket_aware_flag"].eq(True)
    if "triggered_bucket_count" not in merged.columns:
        merged["triggered_bucket_count"] = 0
    merged["triggered_bucket_count"] = merged["triggered_bucket_count"].fillna(0).astype(int)
    if "triggered_buckets" not in merged.columns:
        merged["triggered_buckets"] = [[] for _ in range(len(merged))]
    merged["triggered_buckets"] = merged["triggered_buckets"].apply(
        lambda x: x if isinstance(x, list) else []
    )
    merged["flagged_negative_bucket_contribution"] = pd.to_numeric(
        merged["flagged_negative_bucket_contribution"], errors="coerce"
    ).fillna(0.0)
    reduction = min(max(float(reduction_fraction), 0.0), 1.0)
    cap_fraction = min(max(float(max_daily_delta_fraction), 0.0), 1.0)
    simulated_returns: list[float] = []
    deltas: list[float] = []
    for row in merged.rename(columns={"return": "return_"}).itertuples(index=False):
        base_return = _safe_float(row.return_)
        if not bool(row.bucket_aware_flag) or base_return >= 0.0:
            simulated_returns.append(base_return)
            deltas.append(0.0)
            continue
        raw_delta = -reduction * min(_safe_float(row.flagged_negative_bucket_contribution), 0.0)
        cap = abs(base_return) * cap_fraction
        delta = min(max(raw_delta, 0.0), cap)
        simulated_returns.append(round(base_return + delta, 12))
        deltas.append(round(delta, 12))
    merged["simulated_return"] = simulated_returns
    merged["bucket_aware_delta"] = deltas
    return merged


def summarize_bucket_aware_simulation(candidate: str, rows: pd.DataFrame) -> dict[str, Any]:
    work = rows[rows["candidate"] == candidate].copy() if "candidate" in rows else rows.copy()
    if work.empty:
        return {
            "candidate": candidate,
            "production_ready": False,
            "not_parameter_tuning": True,
            "can_change_strategy_now": False,
            "diagnosis": "no_rows",
        }
    baseline = _compound(pd.to_numeric(work["return"], errors="coerce").fillna(0.0))
    simulated = _compound(pd.to_numeric(work["simulated_return"], errors="coerce").fillna(0.0))
    flagged = work[work["bucket_aware_flag"].astype(bool)].copy()
    if "triggered_bucket_count" not in work.columns:
        work["triggered_bucket_count"] = 0
        flagged["triggered_bucket_count"] = 0
    if "triggered_buckets" not in work.columns:
        work["triggered_buckets"] = [[] for _ in range(len(work))]
        flagged["triggered_buckets"] = [[] for _ in range(len(flagged))]
    diagnosis = (
        "bucket_aware_improved_target_month"
        if simulated > baseline
        else "bucket_aware_no_improvement"
    )
    return {
        "candidate": candidate,
        "production_ready": False,
        "not_parameter_tuning": True,
        "can_change_strategy_now": False,
        "diagnosis": diagnosis,
        "baseline_compound_return": round(float(baseline), 6),
        "simulated_compound_return": round(float(simulated), 6),
        "simulated_minus_baseline": round(float(simulated - baseline), 6),
        "flagged_day_count": int(len(flagged)),
        "n_days": int(len(work)),
        "net_attribution_delta": round(float(work["bucket_aware_delta"].sum()), 6),
        "worst_flagged_days": [
            {
                "date": str(row.date),
                "baseline_return": round(_safe_float(getattr(row, "return_", 0.0)), 6),
                "simulated_return": round(_safe_float(row.simulated_return), 6),
                "delta": round(_safe_float(row.bucket_aware_delta), 6),
                "triggered_bucket_count": int(row.triggered_bucket_count),
                "triggered_buckets": list(row.triggered_buckets)[:8],
            }
            for row in flagged.rename(columns={"return": "return_"})
            .sort_values("return_")
            .head(8)
            .itertuples(index=False)
        ],
        "required_validation_before_change": [
            "randomized_bucket_aware_flag_baseline",
            "look_ahead_bias_audit_for_lagged_bucket_state",
            "monthly_distribution_check_including_2018_2020_2022_2026",
            "walk_forward_oos_vs_unchanged_v29_baseline",
        ],
    }


def _portfolio_target_rows(timing_payload: dict[str, Any], target_month: str) -> pd.DataFrame:
    daily = pd.DataFrame(timing_payload.get("daily_records", []))
    if daily.empty:
        return daily
    daily["date"] = pd.to_datetime(daily["date"], errors="coerce")
    daily = daily.dropna(subset=["date"])
    daily["month"] = daily["date"].dt.to_period("M").astype(str)
    return daily[daily["month"] == target_month].copy()


def _build_flagged_state(args: argparse.Namespace) -> tuple[pd.DataFrame, dict[str, Any]]:
    rows, state = _trace_rows(args)
    bucket_state = aggregate_daily_bucket_state(rows)
    target_month = str(args.target_month)
    if not rows.empty:
        row_months = pd.to_datetime(rows["date"]).dt.to_period("M").astype(str)
        target_day_count = int(rows[row_months == target_month]["date"].nunique())
    else:
        target_day_count = 0
    hypotheses = json.loads(Path(args.hypothesis_json).read_text(encoding="utf-8"))
    specs_by_candidate = _build_bucket_trigger_specs(
        hypotheses, target_day_count=target_day_count, observation_days=int(args.observation_days)
    )
    flagged_frames: list[pd.DataFrame] = []
    for candidate, specs in specs_by_candidate.items():
        candidate_state = bucket_state[bucket_state["candidate"] == candidate].copy()
        if not candidate_state.empty:
            flagged_frames.append(
                add_lagged_stress_state(
                    candidate_state, specs, observation_days=int(args.observation_days)
                )
            )
    flagged = pd.concat(flagged_frames, ignore_index=True) if flagged_frames else bucket_state
    state["target_day_count"] = target_day_count
    state["selected_stock_row_count"] = int(len(rows))
    state["daily_bucket_state_count"] = int(len(flagged))
    return flagged, state


def build_full_report(args: argparse.Namespace, generated_at: datetime) -> dict[str, Any]:
    flagged_state, state = _build_flagged_state(args)
    date_flags = build_date_level_bucket_flags(flagged_state)
    timing_payload = json.loads(Path(args.timing_json).read_text(encoding="utf-8"))
    portfolio_rows = _portfolio_target_rows(timing_payload, str(args.target_month))
    simulated = simulate_bucket_aware_gate(
        portfolio_rows,
        date_flags,
        reduction_fraction=float(args.reduction_fraction),
        max_daily_delta_fraction=float(args.max_daily_delta_fraction),
    )
    candidates = [item.strip() for item in str(args.candidates).split(",") if item.strip()]
    summaries = [
        summarize_bucket_aware_simulation(candidate, simulated) for candidate in candidates
    ]
    return {
        "ts": generated_at.isoformat(),
        "version": "V29-daily-liquidity-score-bucket-aware-flag-simulation",
        "research_only": True,
        "production_ready": False,
        "not_parameter_tuning": True,
        "target_month": args.target_month,
        "simulation_parameters": {
            "observation_days": int(args.observation_days),
            "reduction_fraction": float(args.reduction_fraction),
            "max_daily_delta_fraction": float(args.max_daily_delta_fraction),
            "lagged_inputs_only": True,
            "bucket_dimensions": ["liquidity_bucket", "score_bucket"],
            "sleeve_proxy_excluded": True,
        },
        "coverage": state,
        "date_flag_count": int(len(date_flags)),
        "summaries": summaries,
        "simulated_daily_records": simulated.to_dict(orient="records"),
        "global_decision": {
            "can_change_strategy_now": False,
            "can_claim_production_ready": False,
            "next_required_step": "randomized_bucket_aware_flag_baseline",
            "reason": "This is still an attribution-level simulation; favorable May behavior must beat a randomized schedule and pass look-ahead/monthly/WF checks.",
        },
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# V29 daily liquidity/score bucket-aware flag simulation",
        "",
        "状态：研究仿真；不调参；不改策略；不解除 production blocker。",
        "",
        f"- target_month: {report['target_month']}",
        f"- production_ready: {str(report['production_ready']).lower()}",
        f"- date_flag_count: {report['date_flag_count']}",
        f"- reduction_fraction: {report['simulation_parameters']['reduction_fraction']}",
        f"- max_daily_delta_fraction: {report['simulation_parameters']['max_daily_delta_fraction']}",
        "",
    ]
    for item in report["summaries"]:
        lines.extend(
            [
                f"## {item['candidate']}",
                "",
                f"- diagnosis: {item['diagnosis']}",
                f"- baseline_compound_return: {item['baseline_compound_return']}",
                f"- simulated_compound_return: {item['simulated_compound_return']}",
                f"- simulated_minus_baseline: {item['simulated_minus_baseline']}",
                f"- flagged_day_count: {item['flagged_day_count']} / {item['n_days']}",
                f"- net_attribution_delta: {item['net_attribution_delta']}",
                "",
            ]
        )
    lines.extend(
        [
            "## Decision",
            "",
            "这只是 bucket-aware attribution-level simulation。下一步必须与 randomized bucket-aware baseline 比较，不能直接进入策略代码。",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    report = build_full_report(args, datetime.now(timezone.utc))
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
