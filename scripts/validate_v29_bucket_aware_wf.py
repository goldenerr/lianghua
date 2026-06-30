#!/usr/bin/env python3
"""Fold-internal V29 bucket-aware walk-forward validation.

This is the clean follow-up after same-month May-2026 bucket-aware validation was
blocked as post-hoc. For each fold, trigger specs are generated from the train
window only, then applied to the OOS window. The output is research-only and does
not change strategy code, production config, or feature passes.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

import pandas as pd
from _paths import RESULTS_DIR
from compare_v29_daily_bucket_aware_random_baseline import run_random_baseline_trials
from diagnose_v29_recent90_cross_section import (
    _load_replay_inputs,
    trace_stock_sleeve_cross_section,
)
from diagnose_v29_recent90_daily_bucket_state import (
    add_lagged_stress_state,
    aggregate_daily_bucket_state,
)
from diagnose_v29_recent90_regime import _trace_meta
from research_v29_portfolio_layer import _portfolio_configs, _sleeve_configs
from simulate_v29_daily_bucket_aware_flag import (
    _compound,
    build_date_level_bucket_flags,
    simulate_bucket_aware_gate,
)
from validate_v29_robustness import _load_research_state

DEFAULT_OUTPUT = RESULTS_DIR / "quant_v29_bucket_aware_wf_validation.json"
DEFAULT_REPORT = Path("docs/research/quant_v29_bucket_aware_wf_validation.md")
DEFAULT_CANDIDATES = "v29_price_meta_longhorizon_guard,v29_price_meta_ultradefensive"
DEFAULT_REDUCTION_FRACTION = 0.5
DEFAULT_MAX_DAILY_DELTA_FRACTION = 0.25


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", default=DEFAULT_CANDIDATES)
    parser.add_argument("--universe", default="data/stock_list.json")
    parser.add_argument("--start-date", default="20060101")
    parser.add_argument("--end-date", default="20260625")
    parser.add_argument("--min-history", type=int, default=252)
    parser.add_argument("--train-days", type=int, default=756)
    parser.add_argument("--test-days", type=int, default=252)
    parser.add_argument("--n-folds", type=int, default=5)
    parser.add_argument("--observation-days", type=int, default=3)
    parser.add_argument("--max-specs-per-type", type=int, default=3)
    parser.add_argument("--random-trials", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20260630)
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


def make_walk_forward_folds(
    dates: pd.DatetimeIndex | pd.Series | list[Any],
    *,
    n_folds: int,
    train_days: int,
    test_days: int,
) -> list[dict[str, Any]]:
    """Create chronological train/OOS folds with no overlap or leakage."""
    index = (
        pd.DatetimeIndex(pd.to_datetime(pd.Series(list(dates)), errors="coerce").dropna())
        .sort_values()
        .unique()
    )
    train_n = int(train_days)
    test_n = int(test_days)
    if len(index) < train_n + test_n:
        return []
    max_folds = max(1, min(int(n_folds), (len(index) - train_n) // test_n))
    first_test_start = len(index) - max_folds * test_n
    folds: list[dict[str, Any]] = []
    for fold in range(max_folds):
        test_start_pos = first_test_start + fold * test_n
        test_end_pos = min(test_start_pos + test_n - 1, len(index) - 1)
        train_end_pos = test_start_pos - 1
        train_start_pos = max(0, train_end_pos - train_n + 1)
        folds.append(
            {
                "fold": fold,
                "train_start": pd.Timestamp(index[train_start_pos]),
                "train_end": pd.Timestamp(index[train_end_pos]),
                "test_start": pd.Timestamp(index[test_start_pos]),
                "test_end": pd.Timestamp(index[test_end_pos]),
                "train_n_days": int(train_end_pos - train_start_pos + 1),
                "test_n_days": int(test_end_pos - test_start_pos + 1),
            }
        )
    return folds


def _monthly_compound_by_candidate(rows: pd.DataFrame) -> pd.DataFrame:
    if rows.empty:
        return pd.DataFrame(columns=["candidate", "month", "portfolio_return", "n_days"])
    work = rows.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work = work.dropna(subset=["date"])
    work["month"] = work["date"].dt.to_period("M").astype(str)
    out: list[dict[str, Any]] = []
    for (candidate, month), group in work.groupby(["candidate", "month"]):
        out.append(
            {
                "candidate": str(candidate),
                "month": str(month),
                "portfolio_return": float(
                    _compound(pd.to_numeric(group["return"], errors="coerce").fillna(0.0))
                ),
                "n_days": int(len(group)),
            }
        )
    return pd.DataFrame(out)


def _monthly_bucket_contributions(bucket_state: pd.DataFrame) -> pd.DataFrame:
    if bucket_state.empty:
        return pd.DataFrame(
            columns=["candidate", "month", "bucket_type", "bucket", "contribution_sum", "n_days"]
        )
    work = bucket_state.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work = work.dropna(subset=["date"])
    work = work[work["bucket_type"].isin(["liquidity_bucket", "score_bucket"])]
    work["month"] = work["date"].dt.to_period("M").astype(str)
    return (
        work.groupby(["candidate", "month", "bucket_type", "bucket"], as_index=False)
        .agg(contribution_sum=("contribution_sum", "sum"), n_days=("date", "nunique"))
        .sort_values(["candidate", "bucket_type", "bucket", "month"])
    )


def build_train_bucket_trigger_specs(
    bucket_state: pd.DataFrame,
    portfolio_rows: pd.DataFrame,
    *,
    candidate: str,
    train_start: pd.Timestamp,
    train_end: pd.Timestamp,
    observation_days: int,
    max_specs_per_type: int,
) -> list[dict[str, Any]]:
    """Generate bucket trigger specs using only the training window."""
    state = bucket_state.copy()
    ports = portfolio_rows.copy()
    if state.empty or ports.empty:
        return []
    state["date"] = pd.to_datetime(state["date"], errors="coerce")
    ports["date"] = pd.to_datetime(ports["date"], errors="coerce")
    state = state[
        (state["candidate"] == candidate)
        & (state["date"] >= train_start)
        & (state["date"] <= train_end)
    ].copy()
    ports = ports[
        (ports["candidate"] == candidate)
        & (ports["date"] >= train_start)
        & (ports["date"] <= train_end)
    ].copy()
    if state.empty or ports.empty:
        return []

    monthly_port = _monthly_compound_by_candidate(ports)
    monthly_bucket = _monthly_bucket_contributions(state)
    if monthly_port.empty or monthly_bucket.empty:
        return []
    merged = monthly_bucket.merge(monthly_port, on=["candidate", "month"], how="inner")
    negative_months = set(monthly_port.loc[monthly_port["portfolio_return"] < 0.0, "month"])
    positive_months = set(monthly_port.loc[monthly_port["portfolio_return"] > 0.0, "month"])
    if not negative_months:
        return []
    avg_train_month_days = max(float(monthly_port["n_days"].mean()), 1.0)
    obs = max(int(observation_days), 1)
    specs: list[dict[str, Any]] = []
    for (bucket_type, bucket), group in merged.groupby(["bucket_type", "bucket"]):
        neg = group[group["month"].isin(negative_months)]
        pos = group[group["month"].isin(positive_months)]
        if neg.empty:
            continue
        neg_mean = float(neg["contribution_sum"].mean())
        pos_mean = float(pos["contribution_sum"].mean()) if not pos.empty else 0.0
        pos_hit = float((pos["contribution_sum"] > 0.0).mean()) if not pos.empty else 0.0
        if neg_mean >= 0.0:
            continue
        stress_score = abs(neg_mean) * (1.0 + max(pos_mean, 0.0)) * max(pos_hit, 0.1)
        specs.append(
            {
                "bucket_type": str(bucket_type),
                "bucket": str(bucket),
                "stress_score": round(float(stress_score), 8),
                "train_negative_mean_contribution": round(float(neg_mean), 8),
                "train_positive_mean_contribution": round(float(pos_mean), 8),
                "train_positive_hit_rate": round(float(pos_hit), 6),
                "train_negative_month_count": int(len(neg)),
                "train_positive_month_count": int(len(pos)),
                "daily_trigger_threshold": round(-abs(neg_mean) / avg_train_month_days * obs, 8),
                "train_start": pd.Timestamp(train_start).date().isoformat(),
                "train_end": pd.Timestamp(train_end).date().isoformat(),
                "oos_data_used": False,
            }
        )
    selected: list[dict[str, Any]] = []
    for bucket_type in ("liquidity_bucket", "score_bucket"):
        candidates = [spec for spec in specs if spec["bucket_type"] == bucket_type]
        selected.extend(
            sorted(candidates, key=lambda item: item["stress_score"], reverse=True)[
                : int(max_specs_per_type)
            ]
        )
    return selected


def summarize_wf_fold(
    candidate: str, fold: dict[str, Any], rows: pd.DataFrame, random_trials: list[dict[str, Any]]
) -> dict[str, Any]:
    work = rows[rows["candidate"] == candidate].copy() if "candidate" in rows else rows.copy()
    baseline = _compound(
        pd.to_numeric(work.get("return", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    )
    simulated = _compound(
        pd.to_numeric(work.get("simulated_return", pd.Series(dtype=float)), errors="coerce").fillna(
            0.0
        )
    )
    observed_delta = float(simulated - baseline)
    values = [_safe_float(item.get("simulated_minus_baseline")) for item in random_trials]
    random_median = float(median(values)) if values else 0.0
    random_mean = float(mean(values)) if values else 0.0
    random_best = max(values) if values else 0.0
    random_worst = min(values) if values else 0.0
    percentile = (
        float(sum(1 for value in values if value <= observed_delta) / len(values))
        if values
        else 0.0
    )
    flagged_count = int(
        work.get("bucket_aware_flag", pd.Series(False, index=work.index)).astype(bool).sum()
    )
    beats_median = observed_delta > random_median
    diagnosis = "wf_oos_improved" if observed_delta > 0.0 else "wf_oos_no_improvement"
    if observed_delta > 0.0 and beats_median:
        diagnosis = "wf_oos_beats_random_median"
    return {
        "candidate": candidate,
        "fold": int(fold.get("fold", 0)),
        "production_ready": False,
        "not_parameter_tuning": True,
        "can_change_strategy_now": False,
        "train_start": _date_str(fold.get("train_start")),
        "train_end": _date_str(fold.get("train_end")),
        "test_start": _date_str(fold.get("test_start")),
        "test_end": _date_str(fold.get("test_end")),
        "baseline_compound_return": round(float(baseline), 6),
        "simulated_compound_return": round(float(simulated), 6),
        "observed_delta": round(float(observed_delta), 6),
        "flagged_day_count": flagged_count,
        "n_days": int(len(work)),
        "random_trial_count": int(len(values)),
        "random_median_delta": round(random_median, 6),
        "random_mean_delta": round(random_mean, 6),
        "random_best_delta": round(random_best, 6),
        "random_worst_delta": round(random_worst, 6),
        "observed_percentile_vs_random": round(percentile, 6),
        "beats_random_median": bool(beats_median),
        "diagnosis": diagnosis,
    }


def _date_str(value: Any) -> str:
    return pd.Timestamp(value).date().isoformat() if value is not None else ""


def _candidate_portfolio_rows(
    candidates: list[str], args: argparse.Namespace
) -> tuple[pd.DataFrame, dict[str, Any]]:
    required_sleeves = sorted(
        {
            sleeve
            for cfg in _portfolio_configs()
            if cfg["name"] in candidates
            for sleeve in cfg["sleeves"]
        }
    )
    sleeve_frame, crisis_returns, carry_returns, close_index, regime, state = _load_research_state(
        universe_path=Path(args.universe),
        min_history=int(args.min_history),
        start_date=str(args.start_date),
        end_date=str(args.end_date),
        required_sleeves=required_sleeves,
        trading_status_path=Path(args.trading_status_path),
        require_trading_status=bool(args.require_trading_status),
    )
    configs = {cfg["name"]: cfg for cfg in _portfolio_configs()}
    frames: list[pd.DataFrame] = []
    for candidate in candidates:
        rows = _trace_meta(
            configs[candidate], sleeve_frame, crisis_returns, carry_returns, close_index, regime
        )
        rows.insert(0, "candidate", candidate)
        frames.append(rows)
    return pd.concat(frames, ignore_index=True), state


def _full_bucket_state(
    candidates: list[str],
    args: argparse.Namespace,
    target_start: pd.Timestamp,
    target_end: pd.Timestamp,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    close, amount, daily_returns, rolling_ic, trading_constraints, factors, state = (
        _load_replay_inputs(args)
    )
    configs = {cfg["name"]: cfg for cfg in _portfolio_configs()}
    sleeve_defs = _sleeve_configs()
    industry_map = state.pop("industry_map")
    row_frames: list[pd.DataFrame] = []
    for candidate in candidates:
        for sleeve_name in configs[candidate]["sleeves"]:
            rows = trace_stock_sleeve_cross_section(
                sleeve_name=sleeve_name,
                config=copy.deepcopy(sleeve_defs[sleeve_name]),
                close=close,
                amount=amount,
                daily_stock_returns=daily_returns,
                factors=factors,
                rolling_ic=rolling_ic,
                industry_map=industry_map,
                target_start=target_start,
                target_end=target_end,
                trading_constraints=trading_constraints,
            )
            if not rows.empty:
                rows.insert(0, "candidate", candidate)
                row_frames.append(rows)
    selected_rows = pd.concat(row_frames, ignore_index=True) if row_frames else pd.DataFrame()
    bucket_state = aggregate_daily_bucket_state(
        selected_rows, bucket_columns=("liquidity_bucket", "score_bucket")
    )
    state["selected_stock_row_count"] = int(len(selected_rows))
    state["daily_bucket_state_count"] = int(len(bucket_state))
    return bucket_state, state


def _run_candidate_fold(
    *,
    candidate: str,
    fold: dict[str, Any],
    portfolio_rows: pd.DataFrame,
    bucket_state: pd.DataFrame,
    observation_days: int,
    max_specs_per_type: int,
    reduction_fraction: float,
    max_daily_delta_fraction: float,
    random_trials: int,
    seed: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], pd.DataFrame]:
    specs = build_train_bucket_trigger_specs(
        bucket_state,
        portfolio_rows,
        candidate=candidate,
        train_start=fold["train_start"],
        train_end=fold["train_end"],
        observation_days=observation_days,
        max_specs_per_type=max_specs_per_type,
    )
    candidate_state = bucket_state[bucket_state["candidate"] == candidate].copy()
    candidate_state["date"] = pd.to_datetime(candidate_state["date"], errors="coerce")
    candidate_state = candidate_state[
        (candidate_state["date"] >= fold["train_start"])
        & (candidate_state["date"] <= fold["test_end"])
    ].copy()
    flagged_state = add_lagged_stress_state(
        candidate_state, specs, observation_days=observation_days
    )
    date_flags = build_date_level_bucket_flags(flagged_state)
    rows = portfolio_rows[portfolio_rows["candidate"] == candidate].copy()
    rows["date"] = pd.to_datetime(rows["date"], errors="coerce")
    oos_rows = rows[
        (rows["date"] >= fold["test_start"]) & (rows["date"] <= fold["test_end"])
    ].copy()
    simulated = simulate_bucket_aware_gate(
        oos_rows,
        date_flags,
        reduction_fraction=reduction_fraction,
        max_daily_delta_fraction=max_daily_delta_fraction,
    )
    trial_rows = run_random_baseline_trials(
        simulated,
        n_trials=random_trials,
        seed=seed + int(fold["fold"]) * 1009,
        reduction_fraction=reduction_fraction,
        max_daily_delta_fraction=max_daily_delta_fraction,
    )
    summary = summarize_wf_fold(candidate, fold, simulated, trial_rows)
    summary["train_spec_count"] = int(len(specs))
    summary["train_specs"] = specs
    summary["oos_data_used_for_specs"] = False
    return summary, trial_rows[:20], simulated


def _summarize_candidate(candidate: str, fold_summaries: list[dict[str, Any]]) -> dict[str, Any]:
    values = [_safe_float(item.get("observed_delta")) for item in fold_summaries]
    beats = [bool(item.get("beats_random_median")) for item in fold_summaries]
    improved = [value > 0.0 for value in values]
    avg_delta = float(mean(values)) if values else 0.0
    positive_rate = float(sum(improved) / len(improved)) if improved else 0.0
    random_win_rate = float(sum(beats) / len(beats)) if beats else 0.0
    blockers: list[str] = []
    if len(fold_summaries) < 5:
        blockers.append("too_few_wf_folds")
    if positive_rate < 0.6:
        blockers.append("oos_positive_fold_rate_below_60pct")
    if random_win_rate < 0.6:
        blockers.append("beats_random_median_fold_rate_below_60pct")
    if avg_delta <= 0.0:
        blockers.append("avg_oos_delta_not_positive")
    return {
        "candidate": candidate,
        "production_ready": False,
        "not_parameter_tuning": True,
        "can_change_strategy_now": False,
        "fold_count": int(len(fold_summaries)),
        "avg_oos_delta": round(avg_delta, 6),
        "positive_fold_rate": round(positive_rate, 6),
        "beats_random_median_fold_rate": round(random_win_rate, 6),
        "passes_research_wf_gate": not blockers,
        "blockers": blockers,
        "folds": fold_summaries,
    }


def build_full_report(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    candidates = [item.strip() for item in str(args.candidates).split(",") if item.strip()]
    portfolio_rows, portfolio_state = _candidate_portfolio_rows(candidates, args)
    date_index = pd.DatetimeIndex(
        pd.to_datetime(portfolio_rows["date"], errors="coerce").dropna().unique()
    ).sort_values()
    folds = make_walk_forward_folds(
        date_index,
        n_folds=int(args.n_folds),
        train_days=int(args.train_days),
        test_days=int(args.test_days),
    )
    if not folds:
        raise RuntimeError("not enough dates for requested fold-internal WF validation")
    bucket_state, bucket_state_meta = _full_bucket_state(
        candidates, args, target_start=folds[0]["train_start"], target_end=folds[-1]["test_end"]
    )
    candidate_summaries: list[dict[str, Any]] = []
    trial_samples: dict[str, list[dict[str, Any]]] = {}
    simulated_samples: list[dict[str, Any]] = []
    for candidate in candidates:
        fold_summaries: list[dict[str, Any]] = []
        for fold in folds:
            summary, trials, simulated = _run_candidate_fold(
                candidate=candidate,
                fold=fold,
                portfolio_rows=portfolio_rows,
                bucket_state=bucket_state,
                observation_days=int(args.observation_days),
                max_specs_per_type=int(args.max_specs_per_type),
                reduction_fraction=float(args.reduction_fraction),
                max_daily_delta_fraction=float(args.max_daily_delta_fraction),
                random_trials=int(args.random_trials),
                seed=int(args.seed),
            )
            fold_summaries.append(summary)
            trial_samples[f"{candidate}_fold_{fold['fold']}"] = trials
            simulated_samples.extend(
                [
                    {str(key): value for key, value in row.items()}
                    for row in simulated.head(5).to_dict(orient="records")
                ]
            )
        candidate_summaries.append(_summarize_candidate(candidate, fold_summaries))
    can_change = False
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V29-fold-internal-bucket-aware-wf-validation",
        "research_only": True,
        "production_ready": False,
        "not_parameter_tuning": True,
        "can_change_strategy_now": can_change,
        "parameters": {
            "candidates": candidates,
            "n_folds": int(args.n_folds),
            "train_days": int(args.train_days),
            "test_days": int(args.test_days),
            "observation_days": int(args.observation_days),
            "max_specs_per_type": int(args.max_specs_per_type),
            "random_trials": int(args.random_trials),
            "reduction_fraction": float(args.reduction_fraction),
            "max_daily_delta_fraction": float(args.max_daily_delta_fraction),
            "fold_internal_trigger_generation": True,
            "oos_data_used_for_specs": False,
        },
        "coverage": {
            "portfolio_state": portfolio_state.get("coverage", portfolio_state),
            "bucket_state": bucket_state_meta,
            "portfolio_row_count": int(len(portfolio_rows)),
            "daily_bucket_state_count": int(len(bucket_state)),
            "folds": [
                {
                    **fold,
                    "train_start": _date_str(fold["train_start"]),
                    "train_end": _date_str(fold["train_end"]),
                    "test_start": _date_str(fold["test_start"]),
                    "test_end": _date_str(fold["test_end"]),
                }
                for fold in folds
            ],
        },
        "summaries": candidate_summaries,
        "trial_samples_first20": trial_samples,
        "simulated_daily_sample": simulated_samples[:40],
        "global_decision": {
            "can_change_strategy_now": False,
            "can_claim_production_ready": False,
            "reason": "This validates only fold-internal research timing. Production remains blocked by external evidence/paper/live gates even if research folds pass.",
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
        "# V29 fold-internal bucket-aware WF validation",
        "",
        "状态：研究验证；fold 内训练生成阈值；不调参；不改策略；不解除 production blocker。",
        "",
        f"- production_ready: {str(report['production_ready']).lower()}",
        f"- can_change_strategy_now: {str(report['can_change_strategy_now']).lower()}",
        f"- fold_internal_trigger_generation: {str(report['parameters']['fold_internal_trigger_generation']).lower()}",
        f"- oos_data_used_for_specs: {str(report['parameters']['oos_data_used_for_specs']).lower()}",
        "",
    ]
    for item in report["summaries"]:
        lines.extend(
            [
                f"## {item['candidate']}",
                "",
                f"- passes_research_wf_gate: {str(item['passes_research_wf_gate']).lower()}",
                f"- avg_oos_delta: {item['avg_oos_delta']}",
                f"- positive_fold_rate: {item['positive_fold_rate']}",
                f"- beats_random_median_fold_rate: {item['beats_random_median_fold_rate']}",
                f"- blockers: {item['blockers']}",
                "",
            ]
        )
        for fold in item["folds"]:
            lines.append(
                f"- fold {fold['fold']}: delta={fold['observed_delta']}, flags={fold['flagged_day_count']}, "
                f"random_median={fold['random_median_delta']}, beats_random={str(fold['beats_random_median']).lower()}, "
                f"specs={fold['train_spec_count']}, test={fold['test_start']}..{fold['test_end']}"
            )
        lines.append("")
    lines.extend(
        [
            "## Decision",
            "",
            report["global_decision"]["reason"],
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    report = build_full_report(args)
    _write_json(Path(args.output_json), report)
    _write_markdown(Path(args.report_md), report)
    print(
        json.dumps(
            {
                "production_ready": False,
                "can_change_strategy_now": False,
                "output": args.output_json,
                "report": args.report_md,
                "elapsed_seconds": report["elapsed_seconds"],
                "summaries": [
                    {
                        "candidate": item["candidate"],
                        "passes_research_wf_gate": item["passes_research_wf_gate"],
                        "avg_oos_delta": item["avg_oos_delta"],
                        "positive_fold_rate": item["positive_fold_rate"],
                        "beats_random_median_fold_rate": item["beats_random_median_fold_rate"],
                        "blockers": item["blockers"],
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
