#!/usr/bin/env python3
"""Compare May-2026 failed V29 buckets against historical winning months.

This research-only diagnostic extends the May-2026 cross-section attribution: it
replays the same selected-stock sleeves across a historical window and asks
whether May's losing industries/liquidity/score/sleeve buckets are also normally
positive in historically profitable months. It reports evidence only; it does not
change thresholds, factor signs or exposures.
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
    group_contribution_summary,
    trace_stock_sleeve_cross_section,
)
from research_v29_portfolio_layer import _portfolio_configs, _sleeve_configs

DEFAULT_OUTPUT = RESULTS_DIR / "quant_v29_recent90_historical_contrast_may2026.json"
DEFAULT_REPORT = Path("docs/research/quant_v29_recent90_historical_contrast_may2026.md")
DEFAULT_CANDIDATES = "v29_price_meta_longhorizon_guard,v29_price_meta_ultradefensive"
CONTRAST_COLUMNS = ("industry", "liquidity_bucket", "score_bucket", "sleeve")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", default=DEFAULT_CANDIDATES)
    parser.add_argument("--universe", default="data/stock_list.json")
    parser.add_argument("--start-date", default="20170101")
    parser.add_argument("--end-date", default="20260625")
    parser.add_argument("--target-month", default="2026-05")
    parser.add_argument("--min-history", type=int, default=252)
    parser.add_argument(
        "--trading-status-path",
        default="data/security_master/free_pit_approx/trading_status_v31.parquet",
    )
    parser.add_argument("--require-trading-status", action="store_true", default=True)
    parser.add_argument("--max-buckets", type=int, default=6)
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--report-md", default=str(DEFAULT_REPORT))
    return parser.parse_args()


def _safe_float(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def monthly_bucket_contributions(frame: pd.DataFrame, bucket_col: str) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(
            columns=["month", "bucket", "contribution_sum", "row_count", "symbol_count"]
        )
    work = frame.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work = work.dropna(subset=["date"])
    work["month"] = work["date"].dt.to_period("M").astype(str)
    work["bucket"] = work[bucket_col].fillna("unknown").astype(str)
    grouped = (
        work.groupby(["month", "bucket"], as_index=False)
        .agg(
            contribution_sum=("contribution", "sum"),
            row_count=("contribution", "size"),
            symbol_count=(
                ("symbol", "nunique") if "symbol" in work.columns else ("contribution", "size")
            ),
        )
        .sort_values(["month", "contribution_sum"])
    )
    grouped["contribution_sum"] = grouped["contribution_sum"].round(6)
    return grouped


def _monthly_totals(frame: pd.DataFrame) -> pd.DataFrame:
    work = frame.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work = work.dropna(subset=["date"])
    work["month"] = work["date"].dt.to_period("M").astype(str)
    return (
        work.groupby("month", as_index=False)
        .agg(raw_contribution=("contribution", "sum"), row_count=("contribution", "size"))
        .sort_values("month")
    )


def contrast_bucket_against_positive_months(
    monthly: pd.DataFrame,
    *,
    target_month: str,
    bucket: str,
    positive_months: set[str],
) -> dict[str, Any]:
    target_rows = monthly[(monthly["month"] == target_month) & (monthly["bucket"] == bucket)]
    target_contribution = (
        float(target_rows["contribution_sum"].sum()) if not target_rows.empty else 0.0
    )
    history = monthly[
        (monthly["bucket"] == bucket) & (monthly["month"].isin(positive_months))
    ].copy()
    values = pd.to_numeric(history["contribution_sum"], errors="coerce").dropna()
    positive_mean = float(values.mean()) if not values.empty else 0.0
    positive_median = float(values.median()) if not values.empty else 0.0
    positive_hit_rate = float((values > 0).mean()) if not values.empty else 0.0
    direction_flip = target_contribution < 0 and positive_mean > 0
    if direction_flip and positive_hit_rate >= 0.6:
        diagnosis = "target_negative_vs_positive_history"
    elif target_contribution < 0 and positive_mean <= 0:
        diagnosis = "historically_weak_bucket"
    elif target_contribution < 0:
        diagnosis = "mixed_history_negative_target"
    else:
        diagnosis = "target_not_negative"
    return {
        "bucket": str(bucket),
        "target_contribution": round(target_contribution, 6),
        "positive_month_count": int(len(values)),
        "historical_positive_mean": round(positive_mean, 6),
        "historical_positive_median": round(positive_median, 6),
        "historical_positive_hit_rate": round(positive_hit_rate, 6),
        "direction_flip": bool(direction_flip),
        "diagnosis": diagnosis,
    }


def build_candidate_historical_contrast(
    candidate: str,
    rows: pd.DataFrame,
    *,
    target_month: str,
    max_buckets: int = 6,
) -> dict[str, Any]:
    if rows.empty:
        return {
            "candidate": candidate,
            "production_ready": False,
            "not_parameter_tuning": True,
            "target_month": target_month,
            "diagnosis": "no_rows",
            "next_required_diagnostics": [
                "Verify historical replay rows before interpreting contrast."
            ],
        }
    work = rows.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work = work.dropna(subset=["date"])
    work["month"] = work["date"].dt.to_period("M").astype(str)
    month_totals = _monthly_totals(work)
    positive_months = set(
        month_totals[
            (month_totals["month"] != target_month) & (month_totals["raw_contribution"] > 0)
        ]["month"].astype(str)
    )
    target_rows = work[work["month"] == target_month]
    contrasts: dict[str, list[dict[str, Any]]] = {}
    for column in CONTRAST_COLUMNS:
        if column not in work.columns:
            contrasts[column] = []
            continue
        monthly = monthly_bucket_contributions(work, column)
        worst_target = group_contribution_summary(target_rows, column, limit=max_buckets)
        contrasts[column] = [
            contrast_bucket_against_positive_months(
                monthly,
                target_month=target_month,
                bucket=str(item[column]),
                positive_months=positive_months,
            )
            for item in worst_target
        ]
    target_total = float(target_rows["contribution"].sum()) if not target_rows.empty else 0.0
    direction_flip_count = sum(
        1 for values in contrasts.values() for item in values if item.get("direction_flip")
    )
    diagnosis = (
        "failed_buckets_flip_vs_positive_history"
        if direction_flip_count > 0
        else "failed_buckets_not_positive_historical_edge"
    )
    return {
        "candidate": candidate,
        "production_ready": False,
        "not_parameter_tuning": True,
        "target_month": target_month,
        "diagnosis": diagnosis,
        "target_raw_contribution": round(target_total, 6),
        "month_count": int(month_totals["month"].nunique()),
        "positive_month_count": int(len(positive_months)),
        "contrasts": contrasts,
        "monthly_totals_tail": [
            {
                "month": str(row.month),
                "raw_contribution": round(float(row.raw_contribution), 6),
                "row_count": int(row.row_count),
            }
            for row in month_totals.tail(12).itertuples(index=False)
        ],
        "next_required_diagnostics": [
            "If failed buckets flip from positive history, design a sleeve/bucket-aware risk flag as a hypothesis only, then validate by WF/random/look-ahead/monthly checks.",
            "If buckets are historically weak, investigate stale alpha definitions before changing market regime thresholds.",
            "Do not change factor signs, top_n, crisis weights or exposure thresholds from this contrast alone.",
        ],
    }


def _trace_candidate_rows(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    close, amount, daily_returns, rolling_ic, trading_constraints, factors, state = (
        _load_replay_inputs(args)
    )
    portfolio_configs = {cfg["name"]: cfg for cfg in _portfolio_configs()}
    sleeve_defs = _sleeve_configs()
    candidates = [item.strip() for item in str(args.candidates).split(",") if item.strip()]
    start = pd.Timestamp(datetime.strptime(args.start_date, "%Y%m%d")).normalize()
    end = pd.Timestamp(datetime.strptime(args.end_date, "%Y%m%d")).normalize()
    industry_map = state.pop("industry_map")
    reports: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate not in portfolio_configs:
            raise RuntimeError(f"unknown candidate: {candidate}")
        candidate_frames: list[pd.DataFrame] = []
        for sleeve_name in portfolio_configs[candidate]["sleeves"]:
            rows = trace_stock_sleeve_cross_section(
                sleeve_name=sleeve_name,
                config=copy.deepcopy(sleeve_defs[sleeve_name]),
                close=close,
                amount=amount,
                daily_stock_returns=daily_returns,
                factors=factors,
                rolling_ic=rolling_ic,
                industry_map=industry_map,
                target_start=start,
                target_end=end,
                trading_constraints=trading_constraints,
            )
            if not rows.empty:
                rows.insert(0, "candidate", candidate)
                candidate_frames.append(rows)
        combined = (
            pd.concat(candidate_frames, ignore_index=True) if candidate_frames else pd.DataFrame()
        )
        reports.append(
            build_candidate_historical_contrast(
                candidate,
                combined,
                target_month=args.target_month,
                max_buckets=int(args.max_buckets),
            )
        )
    return reports, state


def build_full_report(args: argparse.Namespace, generated_at: datetime) -> dict[str, Any]:
    started = time.perf_counter()
    reports, state = _trace_candidate_rows(args)
    return {
        "ts": generated_at.isoformat(),
        "version": "V29-recent90-historical-bucket-contrast-may2026",
        "research_only": True,
        "production_ready": False,
        "not_parameter_tuning": True,
        "universe": str(args.universe),
        "target_month": args.target_month,
        "window": {"start_date": args.start_date, "end_date": args.end_date},
        "coverage": state,
        "candidates": reports,
        "decision": {
            "can_change_strategy_now": False,
            "diagnostic_class": "historical failed-bucket contrast, not parameter tuning",
            "reason": "Need to distinguish a one-off regime miss from historically unstable buckets before any strategy hypothesis.",
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
        "# V29 recent-90 May-2026 historical failed-bucket contrast",
        "",
        "状态：研究诊断；不调参；不解除生产 blocker。",
        "",
        f"- target_month: {report['target_month']}",
        f"- production_ready: {str(report['production_ready']).lower()}",
        f"- window: {report['window']['start_date']} -> {report['window']['end_date']}",
        "",
    ]
    for candidate in report["candidates"]:
        lines.extend(
            [
                f"## {candidate['candidate']}",
                "",
                f"- diagnosis: {candidate['diagnosis']}",
                f"- target_raw_contribution: {candidate['target_raw_contribution']}",
                f"- positive_month_count: {candidate['positive_month_count']} / {candidate['month_count']}",
                "",
            ]
        )
        for column in CONTRAST_COLUMNS:
            top = candidate.get("contrasts", {}).get(column, [])[:3]
            summary = ", ".join(
                f"{item['bucket']} target={item['target_contribution']} hist_mean={item['historical_positive_mean']} flip={item['direction_flip']}"
                for item in top
            )
            lines.append(f"- {column}: {summary}")
        lines.append("")
    lines.extend(
        [
            "## Decision",
            "",
            "仍然不能调参。该报告只区分 May-2026 失败 bucket 是历史上通常有正贡献但本月翻负，还是本身长期弱势。任何后续假设必须再跑 WF/random/look-ahead/monthly checks。",
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
                "target_month": args.target_month,
                "output": args.output_json,
                "report": args.report_md,
                "diagnoses": [item["diagnosis"] for item in report["candidates"]],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
