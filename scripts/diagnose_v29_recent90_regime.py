#!/usr/bin/env python3
"""Diagnose V29 recent-90 weakness without parameter tuning.

This research-only diagnostic replays a selected V29 meta portfolio and attributes
its latest 90 trading days by month, sleeve, crisis/carry sleeve, costs, exposure
and risk-off state. It intentionally does not change any strategy parameter or
production readiness gate.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from _paths import RESULTS_DIR
from research_v29_portfolio_layer import (
    _allocate_from_history,
    _market_regime,
    _metrics,
    _portfolio_configs,
)
from validate_v29_robustness import _load_research_state

DEFAULT_OUT = RESULTS_DIR / "quant_v29_recent90_regime_diagnostic.json"
DEFAULT_CSV = RESULTS_DIR / "quant_v29_recent90_regime_diagnostic_daily.csv"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidates", default="v29_price_meta_longhorizon_guard,v29_price_meta_ultradefensive"
    )
    parser.add_argument("--universe", default="data/stock_list.json")
    parser.add_argument("--start-date", default="20060101")
    parser.add_argument("--end-date", default="20260625")
    parser.add_argument(
        "--trading-status-path",
        default="data/security_master/free_pit_approx/trading_status_v31.parquet",
    )
    parser.add_argument("--require-trading-status", action="store_true")
    parser.add_argument("--recent-days", type=int, default=90)
    parser.add_argument("--output-json", default=str(DEFAULT_OUT))
    parser.add_argument("--output-csv", default=str(DEFAULT_CSV))
    return parser.parse_args()


def _safe_float(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def _compound(series: pd.Series) -> float:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty:
        return 0.0
    return float((1.0 + clean).prod() - 1.0)


def _trace_meta(
    config: dict[str, Any],
    sleeve_returns: pd.DataFrame,
    crisis_returns: pd.Series,
    carry_returns: pd.Series,
    close_index: pd.DatetimeIndex,
    regime: dict[str, np.ndarray],
) -> pd.DataFrame:
    selected = sleeve_returns[list(config["sleeves"])].dropna(how="any")
    crisis = crisis_returns.reindex(selected.index).fillna(0.0)
    carry = carry_returns.reindex(selected.index).fillna(0.0)
    old_scaled = pd.Series(0.0, index=selected.columns, dtype=float)
    old_crisis_weight = 0.0
    old_carry_weight = 0.0
    alloc = pd.Series(1.0 / len(selected.columns), index=selected.columns, dtype=float)
    equity = 1.0
    peak_equity = 1.0
    cost_rate = float(config.get("meta_cost_bps", 2)) / 10000.0
    start = max(int(config.get("lookback", 126)), 60)
    rows: list[dict[str, Any]] = []
    for i in range(start, len(selected)):
        date = pd.Timestamp(selected.index[i])
        if (i - start) % int(config["rebalance_freq"]) == 0:
            history = selected.iloc[max(0, i - int(config["lookback"])) : i]
            alloc = _allocate_from_history(history, config)
        close_loc = (
            int(close_index.get_loc(date))
            if date in close_index
            else min(i + 252, len(close_index) - 1)
        )
        gross, risk_off, market_vol = _market_regime(regime, close_loc, config)
        gross = min(float(config.get("base_gross", gross)), gross) if risk_off else gross
        if market_vol > 1e-8 and config.get("vol_target") is not None:
            gross *= min(1.0, float(config["vol_target"]) / market_vol)
        current_dd = (equity - peak_equity) / peak_equity if peak_equity > 0 else 0.0
        mdd_state = "normal"
        if current_dd < -float(config.get("dd_stop_threshold", 1.0)):
            gross *= float(config.get("dd_stop_scale", 0.0))
            mdd_state = "stop_scale"
        elif current_dd < -float(config.get("dd_reduce_threshold", 1.0)):
            gross *= float(config.get("dd_reduce_scale", 0.5))
            mdd_state = "reduce_scale"
        gross = max(0.0, min(1.0, gross))
        residual_weight = 1.0 - gross if bool(config.get("use_crisis_sleeve", True)) else 0.0
        crisis_fraction = max(0.0, min(1.0, float(config.get("crisis_fraction", 1.0))))
        carry_fraction = max(0.0, min(1.0, float(config.get("carry_fraction", 0.0))))
        normalizer = crisis_fraction + carry_fraction
        if normalizer > 1.0:
            crisis_fraction /= normalizer
            carry_fraction /= normalizer
        crisis_weight = residual_weight * crisis_fraction
        carry_weight = residual_weight * carry_fraction
        scaled = alloc * gross
        turnover = (
            float((scaled - old_scaled).abs().sum())
            + abs(crisis_weight - old_crisis_weight)
            + abs(carry_weight - old_carry_weight)
        )
        cost = turnover * cost_rate
        sleeve_contrib = {
            f"contrib_{name}": float(selected.iloc[i][name] * scaled[name])
            for name in selected.columns
        }
        crisis_contrib = crisis_weight * float(crisis.iloc[i])
        carry_contrib = carry_weight * float(carry.iloc[i])
        net_return = sum(sleeve_contrib.values()) + crisis_contrib + carry_contrib - cost
        equity *= 1.0 + net_return
        peak_equity = max(peak_equity, equity)
        rows.append(
            {
                "date": date.date().isoformat(),
                "return": float(net_return),
                "gross_stock_exposure": float(scaled.abs().sum()),
                "crisis_weight": float(crisis_weight),
                "carry_weight": float(carry_weight),
                "risk_off": bool(risk_off),
                "market_vol": float(market_vol),
                "drawdown_before_return": float(current_dd),
                "mdd_state": mdd_state,
                "turnover": float(turnover),
                "cost": float(cost),
                "crisis_contrib": float(crisis_contrib),
                "carry_contrib": float(carry_contrib),
                **sleeve_contrib,
            }
        )
        old_scaled = scaled
        old_crisis_weight = crisis_weight
        old_carry_weight = carry_weight
    return pd.DataFrame(rows)


def _monthly(rows: pd.DataFrame, contrib_cols: list[str]) -> list[dict[str, Any]]:
    if rows.empty:
        return []
    work = rows.copy()
    work["date"] = pd.to_datetime(work["date"])
    work["month"] = work["date"].dt.to_period("M").astype(str)
    out = []
    for month, group in work.groupby("month"):
        item = {
            "month": month,
            "return": round(_compound(group["return"]), 6),
            "avg_stock_exposure": round(float(group["gross_stock_exposure"].mean()), 4),
            "avg_crisis_weight": round(float(group["crisis_weight"].mean()), 4),
            "avg_carry_weight": round(float(group["carry_weight"].mean()), 4),
            "risk_off_ratio": round(float(group["risk_off"].mean()), 4),
            "cost_sum": round(float(group["cost"].sum()), 6),
        }
        for col in contrib_cols:
            item[col] = round(float(group[col].sum()), 6)
        out.append(item)
    return out


def _candidate_summary(name: str, rows: pd.DataFrame, recent_days: int) -> dict[str, Any]:
    rows = rows.copy()
    rows["date"] = pd.to_datetime(rows["date"])
    recent = rows.tail(recent_days).copy()
    contrib_cols = [col for col in recent.columns if col.startswith("contrib_")] + [
        "crisis_contrib",
        "carry_contrib",
    ]
    contrib_totals = {col: round(float(recent[col].sum()), 6) for col in contrib_cols}
    sorted_contrib = sorted(contrib_totals.items(), key=lambda item: item[1])
    worst = recent.nsmallest(10, "return")[
        [
            "date",
            "return",
            "gross_stock_exposure",
            "crisis_weight",
            "carry_weight",
            "risk_off",
            "mdd_state",
            "cost",
        ]
    ]
    best = recent.nlargest(5, "return")[
        [
            "date",
            "return",
            "gross_stock_exposure",
            "crisis_weight",
            "carry_weight",
            "risk_off",
            "mdd_state",
            "cost",
        ]
    ]
    return {
        "candidate": name,
        "recent_days": int(len(recent)),
        "recent_start": recent["date"].min().date().isoformat() if not recent.empty else None,
        "recent_end": recent["date"].max().date().isoformat() if not recent.empty else None,
        "recent_metrics": _metrics(
            pd.Series(recent["return"].to_numpy(), index=pd.DatetimeIndex(recent["date"]))
        ),
        "full_metrics": _metrics(
            pd.Series(rows["return"].to_numpy(), index=pd.DatetimeIndex(rows["date"]))
        ),
        "avg_stock_exposure_recent": (
            round(float(recent["gross_stock_exposure"].mean()), 4) if not recent.empty else 0.0
        ),
        "avg_crisis_weight_recent": (
            round(float(recent["crisis_weight"].mean()), 4) if not recent.empty else 0.0
        ),
        "avg_carry_weight_recent": (
            round(float(recent["carry_weight"].mean()), 4) if not recent.empty else 0.0
        ),
        "risk_off_ratio_recent": (
            round(float(recent["risk_off"].mean()), 4) if not recent.empty else 0.0
        ),
        "mdd_state_counts_recent": recent["mdd_state"].value_counts().to_dict(),
        "contribution_totals_recent": contrib_totals,
        "largest_negative_contributors_recent": [
            {"source": k, "contribution_sum": v} for k, v in sorted_contrib[:5]
        ],
        "monthly_recent": _monthly(recent, contrib_cols),
        "worst_days_recent": [
            {**{k: (v.date().isoformat() if hasattr(v, "date") else v) for k, v in row.items()}}
            for row in worst.to_dict(orient="records")
        ],
        "best_days_recent": [
            {**{k: (v.date().isoformat() if hasattr(v, "date") else v) for k, v in row.items()}}
            for row in best.to_dict(orient="records")
        ],
    }


def main() -> None:
    args = _parse_args()
    started = time.perf_counter()
    configs = {cfg["name"]: cfg for cfg in _portfolio_configs()}
    names = [item.strip() for item in args.candidates.split(",") if item.strip()]
    missing = [name for name in names if name not in configs]
    if missing:
        raise SystemExit(f"unknown candidates: {missing}")
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
    summaries = []
    daily_frames = []
    for name in names:
        trace = _trace_meta(
            configs[name], sleeve_frame, crisis_returns, carry_returns, close_index, regime
        )
        trace.insert(0, "candidate", name)
        daily_frames.append(trace)
        summaries.append(_candidate_summary(name, trace, args.recent_days))
    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V29-recent90-regime-diagnostic",
        "research_only": True,
        "production_ready": False,
        "not_parameter_tuning": True,
        "universe": args.universe,
        "window": {
            "start_date": args.start_date,
            "end_date": args.end_date,
            "recent_days": args.recent_days,
        },
        "coverage": state.get("coverage", {}),
        "trading_constraint_summary": state.get("trading_constraint_summary", {}),
        "summaries": summaries,
        "diagnostic_conclusion": [
            "Recent-90 weakness is treated as a regime/risk diagnosis input, not a trigger for small parameter tuning.",
            "Production readiness remains false regardless of diagnostic output until V30/V44/V48 and 90-day paper evidence pass.",
        ],
        "elapsed_seconds": round(time.perf_counter() - started, 2),
    }
    output_json = Path(args.output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    output_csv = Path(args.output_csv)
    pd.concat(daily_frames, ignore_index=True).to_csv(output_csv, index=False)
    print(
        json.dumps(
            {"output": str(output_json), "daily_csv": str(output_csv), "summaries": summaries},
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
