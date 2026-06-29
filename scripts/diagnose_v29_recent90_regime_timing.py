#!/usr/bin/env python3
"""Audit V29 May-2026 regime-trigger timing without parameter tuning.

This research-only diagnostic joins V29 daily meta-portfolio traces with the
underlying market-regime inputs (20d momentum, 60d momentum, 20d volatility) to
check whether the May-2026 loss window was recognized as risk-off before losses
arrived. It only reports evidence; it does not change thresholds, factor signs or
portfolio exposure.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from _paths import RESULTS_DIR
from diagnose_v29_recent90_regime import _compound, _trace_meta
from research_v29_portfolio_layer import _portfolio_configs
from validate_v29_robustness import _load_research_state

DEFAULT_OUTPUT = RESULTS_DIR / "quant_v29_recent90_regime_timing_may2026.json"
DEFAULT_REPORT = Path("docs/research/quant_v29_recent90_regime_timing_may2026.md")
DEFAULT_CANDIDATES = "v29_price_meta_longhorizon_guard,v29_price_meta_ultradefensive"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", default=DEFAULT_CANDIDATES)
    parser.add_argument("--universe", default="data/stock_list.json")
    parser.add_argument("--start-date", default="20060101")
    parser.add_argument("--end-date", default="20260625")
    parser.add_argument("--target-month", default="2026-05")
    parser.add_argument("--pre-days", type=int, default=20)
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


def classify_regime_state(mom_20: float, mom_60: float, vol_20: float) -> dict[str, Any]:
    """Classify regime using the same documented V28/V29 threshold logic."""
    mom_20 = _safe_float(mom_20)
    mom_60 = _safe_float(mom_60)
    vol_20 = _safe_float(vol_20)
    if mom_20 < -0.03:
        return {"risk_off": True, "regime": "risk_off", "reason": "mom_20_breach"}
    if mom_60 < -0.08:
        return {"risk_off": True, "regime": "risk_off", "reason": "mom_60_breach"}
    if vol_20 > 0.34:
        return {"risk_off": True, "regime": "risk_off", "reason": "vol_20_breach"}
    if mom_20 > 0.03 and mom_60 > 0.02 and vol_20 < 0.28:
        return {"risk_off": False, "regime": "bull", "reason": "bull_conditions"}
    return {"risk_off": False, "regime": "neutral", "reason": "none"}


def _regime_input_rows(close_index: pd.DatetimeIndex, regime: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for loc, date in enumerate(close_index):
        mom_20 = _safe_float(regime["mom_20"][loc])
        mom_60 = _safe_float(regime["mom_60"][loc])
        vol_20 = _safe_float(regime["vol_20"][loc])
        state = (
            classify_regime_state(mom_20, mom_60, vol_20)
            if loc >= 120
            else {
                "risk_off": True,
                "regime": "warmup_risk_off",
                "reason": "warmup",
            }
        )
        rows.append(
            {
                "date": pd.Timestamp(date).date().isoformat(),
                "mom_20": mom_20,
                "mom_60": mom_60,
                "vol_20": vol_20,
                "regime_input_risk_off": bool(state["risk_off"]),
                "regime": state["regime"],
                "reason": state["reason"],
                "distance_to_mom20_breach": round(mom_20 - -0.03, 6),
                "distance_to_mom60_breach": round(mom_60 - -0.08, 6),
                "distance_to_vol20_breach": round(0.34 - vol_20, 6),
            }
        )
    return pd.DataFrame(rows)


def join_regime_inputs(portfolio_rows: pd.DataFrame, regime_rows: pd.DataFrame) -> pd.DataFrame:
    left = portfolio_rows.copy()
    right = regime_rows.copy()
    left["date"] = pd.to_datetime(left["date"]).dt.strftime("%Y-%m-%d")
    right["date"] = pd.to_datetime(right["date"]).dt.strftime("%Y-%m-%d")
    return left.merge(right, on="date", how="left", validate="many_to_one")


def summarize_timing_window(candidate: str, rows: pd.DataFrame) -> dict[str, Any]:
    if rows.empty:
        return {
            "candidate": candidate,
            "production_ready": False,
            "not_parameter_tuning": True,
            "diagnosis": "no_rows",
            "next_required_diagnostics": [
                "Verify date coverage before interpreting regime timing."
            ],
        }
    work = rows.copy()
    work["return"] = pd.to_numeric(work["return"], errors="coerce").fillna(0.0)
    compound_return = _compound(work["return"])
    risk_off_ratio = float(work["risk_off"].astype(bool).mean())
    input_risk_off_ratio = (
        float(work["regime_input_risk_off"].astype(bool).mean())
        if "regime_input_risk_off" in work
        else risk_off_ratio
    )
    if compound_return < 0 and risk_off_ratio <= 0.05:
        diagnosis = "risk_off_missed_loss_window"
    elif compound_return < 0 and risk_off_ratio < 0.5:
        diagnosis = "risk_off_underreacted_loss_window"
    elif compound_return < 0:
        diagnosis = "risk_off_active_but_insufficient"
    else:
        diagnosis = "no_loss_window"
    worst_days = work.nsmallest(min(8, len(work)), "return")
    reason_counts = work.get("reason", pd.Series(dtype=object)).value_counts().to_dict()
    return {
        "candidate": candidate,
        "production_ready": False,
        "not_parameter_tuning": True,
        "start": str(work["date"].min()),
        "end": str(work["date"].max()),
        "n_days": int(len(work)),
        "compound_return": round(float(compound_return), 6),
        "risk_off_ratio": round(risk_off_ratio, 6),
        "regime_input_risk_off_ratio": round(input_risk_off_ratio, 6),
        "avg_stock_exposure": (
            round(float(work["gross_stock_exposure"].mean()), 6)
            if "gross_stock_exposure" in work
            else None
        ),
        "avg_crisis_weight": (
            round(float(work["crisis_weight"].mean()), 6) if "crisis_weight" in work else None
        ),
        "avg_mom_20": round(float(work["mom_20"].mean()), 6) if "mom_20" in work else None,
        "avg_mom_60": round(float(work["mom_60"].mean()), 6) if "mom_60" in work else None,
        "avg_vol_20": round(float(work["vol_20"].mean()), 6) if "vol_20" in work else None,
        "min_distance_to_mom20_breach": (
            round(float(work["distance_to_mom20_breach"].min()), 6)
            if "distance_to_mom20_breach" in work
            else None
        ),
        "min_distance_to_mom60_breach": (
            round(float(work["distance_to_mom60_breach"].min()), 6)
            if "distance_to_mom60_breach" in work
            else None
        ),
        "min_distance_to_vol20_breach": (
            round(float(work["distance_to_vol20_breach"].min()), 6)
            if "distance_to_vol20_breach" in work
            else None
        ),
        "reason_counts": {str(k): int(v) for k, v in reason_counts.items()},
        "diagnosis": diagnosis,
        "worst_days": [
            {
                "date": str(row.date),
                "return": (
                    round(float(row.return_), 6)
                    if hasattr(row, "return_")
                    else round(float(getattr(row, "return")), 6)
                ),
                "risk_off": bool(row.risk_off),
                "reason": str(getattr(row, "reason", "unknown")),
                "mom_20": round(_safe_float(getattr(row, "mom_20", 0.0)), 6),
                "mom_60": round(_safe_float(getattr(row, "mom_60", 0.0)), 6),
                "vol_20": round(_safe_float(getattr(row, "vol_20", 0.0)), 6),
            }
            for row in worst_days.rename(columns={"return": "return_"}).itertuples(index=False)
        ],
        "next_required_diagnostics": [
            "Audit whether market-regime inputs are too broad because equal-weight market momentum stayed above thresholds during sleeve-specific losses.",
            "Compare May-2026 failed industry/liquidity buckets against historical profitable months before changing thresholds.",
            "If changing regime logic later, validate with full walk-forward, random baseline, look-ahead and monthly distribution checks.",
        ],
    }


def _load_state(
    args: argparse.Namespace, names: list[str]
) -> tuple[pd.DataFrame, pd.Series, pd.Series, pd.DatetimeIndex, dict[str, Any], dict[str, Any]]:
    configs = {cfg["name"]: cfg for cfg in _portfolio_configs()}
    required_sleeves = sorted({sleeve for name in names for sleeve in configs[name]["sleeves"]})
    sleeve_frame, crisis_returns, carry_returns, close_index, regime, state = _load_research_state(
        universe_path=Path(args.universe),
        min_history=252,
        start_date=args.start_date,
        end_date=args.end_date,
        required_sleeves=required_sleeves,
        trading_status_path=Path(args.trading_status_path) if args.trading_status_path else None,
        require_trading_status=bool(args.require_trading_status),
    )
    return sleeve_frame, crisis_returns, carry_returns, close_index, regime, state


def build_full_report(args: argparse.Namespace, generated_at: datetime) -> dict[str, Any]:
    started = time.perf_counter()
    configs = {cfg["name"]: cfg for cfg in _portfolio_configs()}
    names = [item.strip() for item in str(args.candidates).split(",") if item.strip()]
    missing = [name for name in names if name not in configs]
    if missing:
        raise RuntimeError(f"unknown candidates: {missing}")
    sleeve_frame, crisis_returns, carry_returns, close_index, regime, state = _load_state(
        args, names
    )
    target_start = pd.Timestamp(f"{args.target_month}-01")
    target_end = target_start + pd.offsets.MonthEnd(0)
    pre_start = close_index[close_index < target_start][-int(args.pre_days)]
    regime_rows = _regime_input_rows(close_index, regime)
    summaries: list[dict[str, Any]] = []
    daily_frames: list[pd.DataFrame] = []
    for name in names:
        trace = _trace_meta(
            configs[name], sleeve_frame, crisis_returns, carry_returns, close_index, regime
        )
        trace.insert(0, "candidate", name)
        joined = join_regime_inputs(trace, regime_rows)
        joined_dates = pd.to_datetime(joined["date"])
        audit_window = joined[(joined_dates >= pre_start) & (joined_dates <= target_end)].copy()
        target_only = joined[(joined_dates >= target_start) & (joined_dates <= target_end)].copy()
        summaries.append(
            {
                "candidate": name,
                "pre_window": summarize_timing_window(name, audit_window),
                "target_month": summarize_timing_window(name, target_only),
            }
        )
        daily_frames.append(audit_window)
    all_daily = pd.concat(daily_frames, ignore_index=True) if daily_frames else pd.DataFrame()
    return {
        "ts": generated_at.isoformat(),
        "version": "V29-recent90-regime-trigger-timing-may2026",
        "research_only": True,
        "production_ready": False,
        "not_parameter_tuning": True,
        "universe": args.universe,
        "target_month": args.target_month,
        "window": {
            "pre_start": pd.Timestamp(pre_start).date().isoformat(),
            "target_start": target_start.date().isoformat(),
            "target_end": target_end.date().isoformat(),
            "pre_days": int(args.pre_days),
        },
        "coverage": state.get("coverage", {}),
        "trading_constraint_summary": state.get("trading_constraint_summary", {}),
        "summaries": summaries,
        "daily_records": all_daily.to_dict(orient="records"),
        "decision": {
            "can_change_strategy_now": False,
            "diagnostic_class": "regime trigger timing audit, not parameter tuning",
            "reason": "Need to prove whether regime inputs missed May losses before altering thresholds or exposures.",
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
        "# V29 recent-90 May-2026 regime trigger timing audit",
        "",
        "状态：研究诊断；不调参；不解除生产 blocker。",
        "",
        f"- target_month: {report['target_month']}",
        f"- production_ready: {str(report['production_ready']).lower()}",
        f"- pre_start: {report['window']['pre_start']}",
        "",
    ]
    for item in report["summaries"]:
        target = item["target_month"]
        lines.extend(
            [
                f"## {item['candidate']}",
                "",
                f"- diagnosis: {target['diagnosis']}",
                f"- target compound_return: {target['compound_return']}",
                f"- target risk_off_ratio: {target['risk_off_ratio']}",
                f"- avg_stock_exposure: {target['avg_stock_exposure']}",
                f"- avg_crisis_weight: {target['avg_crisis_weight']}",
                f"- avg_mom_20: {target['avg_mom_20']}",
                f"- avg_mom_60: {target['avg_mom_60']}",
                f"- avg_vol_20: {target['avg_vol_20']}",
                f"- reason_counts: {target['reason_counts']}",
                "",
            ]
        )
    lines.extend(
        [
            "## Decision",
            "",
            "仍然不能调阈值或暴露。下一步先证明 equal-weight market regime 输入是否过宽，导致行业/袖子级别亏损没有触发 risk-off。",
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
                "diagnoses": [item["target_month"]["diagnosis"] for item in report["summaries"]],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
