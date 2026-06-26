#!/usr/bin/env python3
"""Diagnose V29 recent-90 sleeve signal decay from daily attribution.

This consumes the daily attribution CSV emitted by diagnose_v29_recent90_regime.py
and compares each sleeve's latest 90 trading days against its own history. It is
research-only and deliberately does not tune parameters or alter production gates.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from _paths import RESULTS_DIR

DEFAULT_DAILY_CSV = RESULTS_DIR / "quant_v29_recent90_regime_diagnostic_daily.csv"
DEFAULT_OUTPUT = RESULTS_DIR / "quant_v29_recent90_signal_decay_report.json"
DEFAULT_REPORT = Path("docs/research/quant_v29_recent90_signal_decay_report.md")
CONTRIB_PREFIXES = ("contrib_price_", "crisis_contrib", "carry_contrib")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daily-csv", default=str(DEFAULT_DAILY_CSV))
    parser.add_argument("--recent-days", type=int, default=90)
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--report-md", default=str(DEFAULT_REPORT))
    return parser.parse_args()


def _safe_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if pd.notna(result) else 0.0


def _percentile_rank(values: pd.Series, observed: float) -> float | None:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    if clean.empty:
        return None
    return round(float((clean <= observed).mean()), 6)


def _max_drawdown(series: pd.Series) -> float:
    clean = pd.to_numeric(series, errors="coerce").fillna(0.0)
    if clean.empty:
        return 0.0
    equity = (1.0 + clean).cumprod()
    peak = equity.cummax()
    dd = equity / peak - 1.0
    return round(float(dd.min()), 6)


def _compound(series: pd.Series) -> float:
    clean = pd.to_numeric(series, errors="coerce").fillna(0.0).astype(float)
    growth = 1.0
    for value in clean.to_numpy(dtype=float):
        growth *= 1.0 + float(value)
    return round(growth - 1.0, 6)


def _contrib_columns(frame: pd.DataFrame) -> list[str]:
    return [
        column
        for column in frame.columns
        if column.startswith("contrib_price_") or column in {"crisis_contrib", "carry_contrib"}
    ]


def _rolling_window_sums(series: pd.Series, window: int) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(0.0).rolling(window).sum().dropna()


def _classify_sleeve(
    *,
    recent_sum: float,
    recent_win_rate: float,
    percentile: float | None,
    worst_month_sum: float,
) -> list[str]:
    flags: list[str] = []
    if recent_sum < 0:
        flags.append("recent_negative")
    if percentile is not None and percentile <= 0.2:
        flags.append("bottom_quintile_vs_history")
    if recent_win_rate < 0.45:
        flags.append("low_recent_hit_rate")
    if worst_month_sum < 0 and abs(worst_month_sum) >= abs(recent_sum) * 0.45:
        flags.append("loss_concentrated_in_worst_month")
    return flags


def analyze_candidate(frame: pd.DataFrame, *, recent_days: int) -> dict[str, Any]:
    frame = frame.sort_values("date").copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"])
    candidate = str(frame["candidate"].iloc[0])
    recent = frame.tail(recent_days).copy()
    contrib_cols = _contrib_columns(frame)
    recent_month = recent["date"].dt.to_period("M").astype(str)
    sleeve_rows: list[dict[str, Any]] = []
    for column in contrib_cols:
        full = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
        recent_series = pd.to_numeric(recent[column], errors="coerce").fillna(0.0)
        rolling_90 = _rolling_window_sums(full, recent_days)
        recent_sum = round(float(recent_series.sum()), 6)
        month_sums = recent_series.groupby(recent_month).sum().sort_values()
        worst_month = str(month_sums.index[0]) if not month_sums.empty else None
        worst_month_sum = round(float(month_sums.iloc[0]), 6) if not month_sums.empty else 0.0
        percentile = _percentile_rank(rolling_90, recent_sum)
        recent_win_rate = round(float((recent_series > 0).mean()), 6) if len(recent_series) else 0.0
        full_win_rate = round(float((full > 0).mean()), 6) if len(full) else 0.0
        sleeve_rows.append(
            {
                "sleeve": column,
                "recent_sum": recent_sum,
                "recent_mean": round(float(recent_series.mean()), 8) if len(recent_series) else 0.0,
                "recent_win_rate": recent_win_rate,
                "full_mean": round(float(full.mean()), 8) if len(full) else 0.0,
                "full_win_rate": full_win_rate,
                "recent_90_sum_percentile_vs_history": percentile,
                "worst_recent_month": worst_month,
                "worst_recent_month_sum": worst_month_sum,
                "flags": _classify_sleeve(
                    recent_sum=recent_sum,
                    recent_win_rate=recent_win_rate,
                    percentile=percentile,
                    worst_month_sum=worst_month_sum,
                ),
            }
        )
    sleeve_rows.sort(key=lambda item: (item["recent_sum"], item["sleeve"]))
    return_rows = pd.to_numeric(recent["return"], errors="coerce").fillna(0.0)
    worst_sleeves = [row for row in sleeve_rows if row["recent_sum"] < 0][:4]
    return {
        "candidate": candidate,
        "recent_days": int(len(recent)),
        "recent_start": recent["date"].min().date().isoformat() if not recent.empty else None,
        "recent_end": recent["date"].max().date().isoformat() if not recent.empty else None,
        "recent_total_return": _compound(return_rows),
        "recent_mdd": _max_drawdown(return_rows),
        "sleeves": sleeve_rows,
        "dominant_negative_sleeves": worst_sleeves,
        "diagnosis": _candidate_diagnosis(worst_sleeves),
        "production_ready": False,
        "not_parameter_tuning": True,
    }


def _candidate_diagnosis(worst_sleeves: list[dict[str, Any]]) -> list[str]:
    diagnosis: list[str] = []
    if any(row["sleeve"].startswith("contrib_price_") for row in worst_sleeves):
        diagnosis.append("stock_alpha_sleeves_are_primary_recent_drag")
    if any("bottom_quintile_vs_history" in row["flags"] for row in worst_sleeves):
        diagnosis.append("recent_drag_is_low_percentile_vs_own_history")
    if any("loss_concentrated_in_worst_month" in row["flags"] for row in worst_sleeves):
        diagnosis.append("losses_are_month_concentrated")
    if not diagnosis:
        diagnosis.append("no_clear_signal_decay_pattern")
    return diagnosis


def build_report(
    daily: pd.DataFrame, *, recent_days: int, generated_at: datetime
) -> dict[str, Any]:
    required = {"candidate", "date", "return"}
    missing = sorted(required - set(daily.columns))
    if missing:
        raise RuntimeError(f"daily attribution CSV missing columns: {missing}")
    daily = daily.copy()
    daily["date"] = pd.to_datetime(daily["date"], errors="coerce")
    daily = daily.dropna(subset=["date", "candidate"])
    candidates = [
        analyze_candidate(group, recent_days=recent_days)
        for _, group in daily.groupby("candidate", sort=True)
    ]
    global_diagnosis = sorted({flag for item in candidates for flag in item["diagnosis"]})
    return {
        "ts": generated_at.isoformat(),
        "version": "V29-recent90-signal-decay-report",
        "research_only": True,
        "production_ready": False,
        "not_parameter_tuning": True,
        "recent_days": recent_days,
        "candidate_count": len(candidates),
        "global_diagnosis": global_diagnosis,
        "candidates": candidates,
        "decision": {
            "should_parameter_tune_now": False,
            "next_step_class": "signal validation/regime trigger audit before any parameter changes",
        },
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# V29 recent-90 signal-decay report",
        "",
        "状态：研究诊断，不调参，不解除生产 blocker。",
        "",
        f"- global_diagnosis: {', '.join(report['global_diagnosis'])}",
        "- should_parameter_tune_now: false",
        "",
    ]
    for candidate in report["candidates"]:
        lines.extend(
            [
                f"## {candidate['candidate']}",
                "",
                f"- recent_total_return: {candidate['recent_total_return']}",
                f"- recent_mdd: {candidate['recent_mdd']}",
                f"- diagnosis: {', '.join(candidate['diagnosis'])}",
                "",
                "### Dominant negative sleeves",
                "",
            ]
        )
        for sleeve in candidate["dominant_negative_sleeves"]:
            lines.append(
                f"- `{sleeve['sleeve']}`: recent_sum={sleeve['recent_sum']}, "
                f"percentile={sleeve['recent_90_sum_percentile_vs_history']}, "
                f"worst_month={sleeve['worst_recent_month']} ({sleeve['worst_recent_month_sum']}), "
                f"flags={','.join(sleeve['flags'])}"
            )
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    daily = pd.read_csv(args.daily_csv)
    report = build_report(
        daily, recent_days=args.recent_days, generated_at=datetime.now(timezone.utc)
    )
    _write_json(Path(args.output_json), report)
    _write_markdown(Path(args.report_md), report)
    print(
        json.dumps(
            {
                "production_ready": False,
                "not_parameter_tuning": True,
                "candidate_count": report["candidate_count"],
                "global_diagnosis": report["global_diagnosis"],
                "output": args.output_json,
                "report": args.report_md,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
