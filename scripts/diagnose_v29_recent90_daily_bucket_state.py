#!/usr/bin/env python3
"""Trace true daily liquidity/score bucket state for V29 recent-90 repair work.

The sleeve-only stress flag was falsified by a randomized baseline. This script
builds the missing daily bucket state directly from selected-stock rows:
liquidity bucket, score bucket and sleeve bucket contribution by date. It is a
research diagnostic only and does not change strategy code, thresholds or
production configuration.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from _paths import RESULTS_DIR
from diagnose_v29_recent90_cross_section import (
    _load_replay_inputs,
    trace_stock_sleeve_cross_section,
)
from research_v29_portfolio_layer import _portfolio_configs, _sleeve_configs

DEFAULT_HYPOTHESIS = RESULTS_DIR / "quant_v29_bucket_risk_flag_hypothesis.json"
DEFAULT_OUTPUT = RESULTS_DIR / "quant_v29_recent90_daily_bucket_state_may2026.json"
DEFAULT_REPORT = Path("docs/research/quant_v29_recent90_daily_bucket_state_may2026.md")
DEFAULT_CANDIDATES = "v29_price_meta_longhorizon_guard,v29_price_meta_ultradefensive"
BUCKET_COLUMNS = ("liquidity_bucket", "score_bucket", "sleeve")


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
    parser.add_argument("--observation-days", type=int, default=3)
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


def aggregate_daily_bucket_state(
    rows: pd.DataFrame, *, bucket_columns: tuple[str, ...] = BUCKET_COLUMNS
) -> pd.DataFrame:
    if rows.empty:
        return pd.DataFrame(
            columns=[
                "candidate",
                "date",
                "bucket_type",
                "bucket",
                "contribution_sum",
                "row_count",
                "symbol_count",
            ]
        )
    frames: list[pd.DataFrame] = []
    for column in bucket_columns:
        if column not in rows.columns:
            continue
        work = rows.copy()
        work["bucket_type"] = column
        work["bucket"] = work[column].fillna("unknown").astype(str)
        grouped = (
            work.groupby(["candidate", "date", "bucket_type", "bucket"], as_index=False)
            .agg(
                contribution_sum=("contribution", "sum"),
                row_count=("contribution", "size"),
                symbol_count=(
                    ("symbol", "nunique") if "symbol" in work.columns else ("contribution", "size")
                ),
            )
            .sort_values(["candidate", "date", "bucket_type", "contribution_sum"])
        )
        grouped["contribution_sum"] = grouped["contribution_sum"].round(8)
        frames.append(grouped)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _spec_key(spec: dict[str, Any]) -> tuple[str, str]:
    return str(spec.get("bucket_type")), str(spec.get("bucket"))


def add_lagged_stress_state(
    state: pd.DataFrame, specs: list[dict[str, Any]], *, observation_days: int
) -> pd.DataFrame:
    out = state.copy().reset_index(drop=True)
    out["bucket_stress_flag"] = False
    out["rolling_prior_contribution"] = 0.0
    out["daily_trigger_threshold"] = None
    if out.empty or not specs:
        out["bucket_stress_flag"] = out["bucket_stress_flag"].astype(object)
        return out
    spec_map = {_spec_key(spec): spec for spec in specs}
    obs = max(int(observation_days), 1)
    flags: list[bool] = []
    rolling_values: list[float] = []
    thresholds: list[float | None] = []
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    out = out.sort_values(["candidate", "bucket_type", "bucket", "date"]).reset_index(drop=True)
    for idx, row in out.iterrows():
        key = (str(row["bucket_type"]), str(row["bucket"]))
        spec = spec_map.get(key)
        if spec is None:
            flags.append(False)
            rolling_values.append(0.0)
            thresholds.append(None)
            continue
        mask = (
            (out["candidate"] == row["candidate"])
            & (out["bucket_type"] == row["bucket_type"])
            & (out["bucket"] == row["bucket"])
        )
        group_indices = out.index[mask].tolist()
        local_pos = group_indices.index(idx)
        prior_indices = group_indices[max(0, local_pos - obs) : local_pos]
        rolling = float(out.loc[prior_indices, "contribution_sum"].sum()) if prior_indices else 0.0
        threshold = _safe_float(spec.get("daily_trigger_threshold"))
        flags.append(bool(prior_indices and rolling <= threshold))
        rolling_values.append(round(rolling, 8))
        thresholds.append(round(threshold, 8))
    out["bucket_stress_flag"] = pd.Series(flags, dtype=object)
    out["rolling_prior_contribution"] = rolling_values
    out["daily_trigger_threshold"] = thresholds
    return out


def _build_bucket_trigger_specs(
    hypotheses: dict[str, Any], *, target_day_count: int, observation_days: int
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    denom = max(int(target_day_count), 1)
    obs = max(int(observation_days), 1)
    for hypothesis in hypotheses.get("hypotheses", []):
        candidate = str(hypothesis.get("candidate"))
        specs: list[dict[str, Any]] = []
        for item in hypothesis.get("ranked_flag_inputs", []):
            bucket_type = str(item.get("bucket_type"))
            if bucket_type not in {"liquidity_bucket", "score_bucket", "sleeve"}:
                continue
            target = _safe_float(item.get("target_contribution"))
            if target >= 0.0:
                continue
            specs.append(
                {
                    "bucket_type": bucket_type,
                    "bucket": str(item.get("bucket")),
                    "stress_score": item.get("stress_score"),
                    "target_contribution": round(target, 6),
                    "daily_trigger_threshold": round(-abs(target) / denom * obs, 8),
                }
            )
        out[candidate] = specs
    return out


def summarize_candidate_bucket_state(candidate: str, flagged: pd.DataFrame) -> dict[str, Any]:
    work = (
        flagged[flagged["candidate"] == candidate].copy()
        if "candidate" in flagged
        else flagged.copy()
    )
    if "rolling_prior_contribution" not in work.columns:
        work["rolling_prior_contribution"] = 0.0
    if "daily_trigger_threshold" not in work.columns:
        work["daily_trigger_threshold"] = None
    if "bucket_stress_flag" not in work.columns:
        work["bucket_stress_flag"] = False
    if work.empty:
        return {
            "candidate": candidate,
            "production_ready": False,
            "not_parameter_tuning": True,
            "can_change_strategy_now": False,
            "diagnosis": "no_rows",
        }
    flagged_rows = work[work["bucket_stress_flag"].astype(bool)].copy()
    worst = work.sort_values("contribution_sum").head(12)
    by_type = (
        work.groupby("bucket_type", as_index=False)
        .agg(
            contribution_sum=("contribution_sum", "sum"),
            flagged_bucket_day_count=("bucket_stress_flag", lambda s: int(s.astype(bool).sum())),
            bucket_count=("bucket", "nunique"),
        )
        .sort_values("contribution_sum")
    )
    return {
        "candidate": candidate,
        "production_ready": False,
        "not_parameter_tuning": True,
        "can_change_strategy_now": False,
        "diagnosis": "daily_bucket_state_available",
        "bucket_day_count": int(len(work)),
        "flagged_bucket_day_count": int(len(flagged_rows)),
        "bucket_type_summary": [
            {
                "bucket_type": str(row.bucket_type),
                "contribution_sum": round(float(row.contribution_sum), 6),
                "flagged_bucket_day_count": int(row.flagged_bucket_day_count),
                "bucket_count": int(row.bucket_count),
            }
            for row in by_type.itertuples(index=False)
        ],
        "worst_bucket_days": [
            {
                "date": str(row.date),
                "bucket_type": str(row.bucket_type),
                "bucket": str(row.bucket),
                "contribution_sum": round(float(row.contribution_sum), 6),
                "rolling_prior_contribution": round(_safe_float(row.rolling_prior_contribution), 6),
                "bucket_stress_flag": bool(row.bucket_stress_flag),
            }
            for row in worst.itertuples(index=False)
        ],
        "next_step": "Use this daily liquidity/score state for a lagged bucket-aware simulation; do not change strategy code from this trace alone.",
    }


def _trace_rows(args: argparse.Namespace) -> tuple[pd.DataFrame, dict[str, Any]]:
    close, amount, daily_returns, rolling_ic, trading_constraints, factors, state = (
        _load_replay_inputs(args)
    )
    target_start = pd.Timestamp(f"{args.target_month}-01")
    target_end = target_start + pd.offsets.MonthEnd(0)
    pre_dates = close.index[close.index < target_start]
    pre_start = (
        pd.Timestamp(pre_dates[-int(args.pre_days)])
        if len(pre_dates) >= int(args.pre_days)
        else target_start
    )
    portfolio_configs = {cfg["name"]: cfg for cfg in _portfolio_configs()}
    sleeve_defs = _sleeve_configs()
    candidates = [item.strip() for item in str(args.candidates).split(",") if item.strip()]
    industry_map = state.pop("industry_map")
    row_frames: list[pd.DataFrame] = []
    for candidate in candidates:
        if candidate not in portfolio_configs:
            raise RuntimeError(f"unknown candidate: {candidate}")
        for sleeve_name in portfolio_configs[candidate]["sleeves"]:
            sleeve_rows = trace_stock_sleeve_cross_section(
                sleeve_name=sleeve_name,
                config=copy.deepcopy(sleeve_defs[sleeve_name]),
                close=close,
                amount=amount,
                daily_stock_returns=daily_returns,
                factors=factors,
                rolling_ic=rolling_ic,
                industry_map=industry_map,
                target_start=pre_start,
                target_end=target_end,
                trading_constraints=trading_constraints,
            )
            if not sleeve_rows.empty:
                sleeve_rows.insert(0, "candidate", candidate)
                row_frames.append(sleeve_rows)
    rows = pd.concat(row_frames, ignore_index=True) if row_frames else pd.DataFrame()
    state["window"] = {
        "pre_start": pre_start.date().isoformat(),
        "target_start": target_start.date().isoformat(),
        "target_end": target_end.date().isoformat(),
        "pre_days": int(args.pre_days),
    }
    return rows, state


def build_full_report(args: argparse.Namespace, generated_at: datetime) -> dict[str, Any]:
    started = time.perf_counter()
    rows, state = _trace_rows(args)
    bucket_state = aggregate_daily_bucket_state(rows)
    target_month = str(args.target_month)
    target_day_count = (
        int(
            rows[pd.to_datetime(rows["date"]).dt.to_period("M").astype(str) == target_month][
                "date"
            ].nunique()
        )
        if not rows.empty
        else 0
    )
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
    candidates = [item.strip() for item in str(args.candidates).split(",") if item.strip()]
    summaries = [summarize_candidate_bucket_state(candidate, flagged) for candidate in candidates]
    top_flagged = (
        flagged[flagged.get("bucket_stress_flag", pd.Series(dtype=bool)).astype(bool)].copy()
        if not flagged.empty
        else pd.DataFrame()
    )
    return {
        "ts": generated_at.isoformat(),
        "version": "V29-recent90-daily-liquidity-score-bucket-state-may2026",
        "research_only": True,
        "production_ready": False,
        "not_parameter_tuning": True,
        "target_month": target_month,
        "coverage": state,
        "observation_days": int(args.observation_days),
        "selected_stock_row_count": int(len(rows)),
        "daily_bucket_state_count": int(len(flagged)),
        "trigger_specs": specs_by_candidate,
        "summaries": summaries,
        "top_flagged_bucket_days": [
            {
                "candidate": str(row.candidate),
                "date": str(row.date),
                "bucket_type": str(row.bucket_type),
                "bucket": str(row.bucket),
                "contribution_sum": round(float(row.contribution_sum), 6),
                "rolling_prior_contribution": round(_safe_float(row.rolling_prior_contribution), 6),
                "daily_trigger_threshold": round(_safe_float(row.daily_trigger_threshold), 6),
            }
            for row in top_flagged.sort_values("rolling_prior_contribution")
            .head(25)
            .itertuples(index=False)
        ],
        "decision": {
            "can_change_strategy_now": False,
            "next_step_class": "daily liquidity/score bucket-aware simulation, still research-only",
            "reason": "The sleeve-only proxy was falsified; this creates the missing daily bucket state needed for a better hypothesis.",
        },
        "elapsed_seconds": round(time.perf_counter() - started, 2),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# V29 recent-90 daily liquidity/score bucket state",
        "",
        "状态：研究诊断；不调参；不改策略；不解除 production blocker。",
        "",
        f"- target_month: {report['target_month']}",
        f"- production_ready: {str(report['production_ready']).lower()}",
        f"- selected_stock_row_count: {report['selected_stock_row_count']}",
        f"- daily_bucket_state_count: {report['daily_bucket_state_count']}",
        "",
    ]
    for item in report["summaries"]:
        lines.extend(
            [
                f"## {item['candidate']}",
                "",
                f"- diagnosis: {item['diagnosis']}",
                f"- bucket_day_count: {item.get('bucket_day_count')}",
                f"- flagged_bucket_day_count: {item.get('flagged_bucket_day_count')}",
                "- bucket_type_summary: "
                + ", ".join(
                    f"{row['bucket_type']} sum={row['contribution_sum']} flags={row['flagged_bucket_day_count']}"
                    for row in item.get("bucket_type_summary", [])
                ),
                "",
            ]
        )
    lines.extend(
        [
            "## Decision",
            "",
            "该报告只补齐 daily liquidity/score bucket state。它不是策略变更；下一步才可做真正 bucket-aware lagged simulation。",
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
                        "bucket_day_count": item.get("bucket_day_count"),
                        "flagged_bucket_day_count": item.get("flagged_bucket_day_count"),
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
