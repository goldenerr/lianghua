#!/usr/bin/env python3
"""Validate V29 candidate robustness before any production discussion.

This is a research gate. It intentionally does not change production readiness;
it checks whether the best V29 portfolio-layer candidate survives parameter,
cost and recent-window stress. External WORM/provider/approval/paper evidence is
still required separately.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from _paths import RESULTS_DIR
from research_v28_factor_direction import (
    DEFAULT_UNIVERSE,
    PRICE_FACTOR_NAMES,
    _build_factors,
    _build_market_regime_features,
    _load_industry_map,
    _load_market_panel,
    _read_codes,
    _rolling_ic_weights,
)
from research_v29_portfolio_layer import (
    DEFAULT_V31_TRADING_STATUS,
    _load_asset_returns,
    _load_crisis_returns,
    _load_trading_status_constraints,
    _metrics,
    _portfolio_configs,
    _run_meta_portfolio,
    _run_stock_sleeve,
    _sleeve_configs,
    _walk_forward_from_returns,
)

DEFAULT_OUTPUT_JSON = RESULTS_DIR / "quant_v29_robustness.json"
DEFAULT_OUTPUT_CSV = RESULTS_DIR / "quant_v29_robustness_cases.csv"
DEFAULT_CANDIDATE = "v29_price_meta_lowvol_small_carry_floor"

LEADING_THRESHOLDS = {
    "full_sharpe_min": 2.00,
    "oos_sharpe_min": 1.50,
    "max_drawdown_min": -0.10,
    "win_rate_min": 0.50,
    "sharpe_decay_max": 0.15,
    "min_oos_sharpe_min": 0.0,
}

ROBUST_THRESHOLDS = {
    "min_sensitivity_full_sharpe": 1.80,
    "min_sensitivity_oos_sharpe": 1.20,
    "min_sensitivity_drawdown": -0.13,
    "min_recent_90_sharpe": 0.50,
    "min_recent_252_sharpe": 0.80,
    "min_cost_stress_full_sharpe": 1.80,
    "min_cost_stress_oos_sharpe": 1.20,
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", default=DEFAULT_CANDIDATE)
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument("--min-history", type=int, default=252)
    parser.add_argument("--start-date", default="20170101")
    parser.add_argument("--end-date", default="20260529")
    parser.add_argument(
        "--trading-status-path",
        default="",
        help=(
            "Optional V31 free trading-status parquet for constrained robustness. "
            f"Suggested path: {DEFAULT_V31_TRADING_STATUS}"
        ),
    )
    parser.add_argument("--require-trading-status", action="store_true")
    return parser.parse_args()


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _fold_min(wf: dict[str, Any], key: str) -> float | None:
    values = [_finite(item.get(key)) for item in wf.get("folds", []) if isinstance(item, dict)]
    values = [value for value in values if value is not None]
    return min(values) if values else None


def _leading_failures(full: dict[str, Any], wf: dict[str, Any]) -> list[str]:
    checks = [
        ("full_sharpe", _finite(full.get("sharpe_ratio")), ">=", LEADING_THRESHOLDS["full_sharpe_min"]),
        ("avg_oos_sharpe", _finite(wf.get("avg_oos_sharpe")), ">=", LEADING_THRESHOLDS["oos_sharpe_min"]),
        ("max_drawdown", _finite(full.get("max_drawdown")), ">=", LEADING_THRESHOLDS["max_drawdown_min"]),
        ("win_rate", _finite(full.get("win_rate")), ">=", LEADING_THRESHOLDS["win_rate_min"]),
        ("sharpe_decay", _finite(wf.get("sharpe_decay")), "<=", LEADING_THRESHOLDS["sharpe_decay_max"]),
        ("min_oos_sharpe", _fold_min(wf, "oos"), ">=", LEADING_THRESHOLDS["min_oos_sharpe_min"]),
    ]
    failures: list[str] = []
    for key, value, op, threshold in checks:
        if value is None:
            failures.append(f"{key}:missing")
            continue
        failed = value < threshold if op == ">=" else value > threshold
        if failed:
            failures.append(f"{key}:{value} {op} {threshold} failed")
    return failures


def _case_record(label: str, config: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    wf = _walk_forward_from_returns(run["returns"])
    full = run["full"]
    failures = _leading_failures(full, wf)
    return {
        "label": label,
        "candidate": config["name"],
        "full": full,
        "wf": wf,
        "leading_passes": not failures,
        "leading_failures": failures,
    }


def _run_case(
    label: str,
    config: dict[str, Any],
    sleeve_frame: pd.DataFrame,
    crisis_returns: pd.Series,
    carry_returns: pd.Series,
    close_index: pd.DatetimeIndex,
    regime: dict[str, np.ndarray],
) -> dict[str, Any]:
    run = _run_meta_portfolio(config, sleeve_frame, crisis_returns, carry_returns, close_index, regime)
    return _case_record(label, config, run)


def _load_research_state(
    *,
    universe_path: Path,
    min_history: int,
    start_date: str,
    end_date: str,
    required_sleeves: list[str],
    cost_multiplier: float = 1.0,
    trading_status_path: Path | None = None,
    require_trading_status: bool = False,
) -> tuple[pd.DataFrame, pd.Series, pd.Series, pd.DatetimeIndex, dict[str, np.ndarray], dict[str, Any]]:
    codes = _read_codes(universe_path)
    close, volume, amount = _load_market_panel(codes, min_history=min_history)
    start = pd.Timestamp(datetime.strptime(start_date, "%Y%m%d")).normalize()
    end = pd.Timestamp(datetime.strptime(end_date, "%Y%m%d")).normalize()
    close = close.loc[(close.index >= start) & (close.index <= end)].copy()
    volume = volume.reindex(close.index)
    amount = amount.reindex(close.index)
    trading_constraints = None
    trading_constraint_summary: dict[str, Any] = {
        "enabled": False,
        "path": None,
        "mode": "legacy_unconstrained_daily_rebalance",
    }
    if trading_status_path is not None:
        trading_constraints, trading_constraint_summary = _load_trading_status_constraints(
            trading_status_path,
            close.index,
            close.columns,
        )
        if require_trading_status and trading_constraints is None:
            raise RuntimeError(f"required trading status constraints are unavailable: {trading_constraint_summary}")
    elif require_trading_status:
        raise RuntimeError("require_trading_status needs trading_status_path")
    daily_stock_returns = close.pct_change(fill_method=None).fillna(0.0)
    regime = _build_market_regime_features(close)
    factors = _build_factors(close, volume, amount)
    future_5d = close.shift(-5) / close - 1.0
    rolling_ic = _rolling_ic_weights(factors, future_5d)
    industry_map = _load_industry_map()
    sleeve_defs = _sleeve_configs()
    sleeve_series: dict[str, pd.Series] = {}
    sleeve_diagnostics: dict[str, Any] = {}
    for name in required_sleeves:
        config = copy.deepcopy(sleeve_defs[name])
        config["cost_bps"] = float(config.get("cost_bps", 10)) * cost_multiplier
        series, diag = _run_stock_sleeve(
            name,
            config,
            close,
            daily_stock_returns,
            factors,
            rolling_ic,
            industry_map,
            trading_constraints=trading_constraints,
        )
        sleeve_series[name] = series
        sleeve_diagnostics[name] = diag
    sleeve_frame = pd.DataFrame(sleeve_series).dropna(how="all")
    crisis_returns = _load_crisis_returns(close.index)
    carry_returns = _load_asset_returns("etf_511260.parquet", close.index)
    state = {
        "coverage": {
            "symbols_loaded": int(close.shape[1]),
            "dates": int(close.shape[0]),
            "sleeve_rows": int(len(sleeve_frame)),
            "price_factor_names": sorted(PRICE_FACTOR_NAMES),
        },
        "sleeve_diagnostics": sleeve_diagnostics,
        "trading_constraint_summary": trading_constraint_summary,
    }
    return sleeve_frame, crisis_returns, carry_returns, close.index, regime, state


def _recent_metrics(returns: pd.Series) -> dict[str, Any]:
    return {
        "last_90": _metrics(returns.tail(90)),
        "last_252": _metrics(returns.tail(252)),
        "last_504": _metrics(returns.tail(504)),
    }


def _write_csv(path: Path, cases: list[dict[str, Any]]) -> None:
    fields = [
        "label",
        "candidate",
        "leading_passes",
        "sharpe_ratio",
        "avg_oos_sharpe",
        "min_oos_sharpe",
        "max_drawdown",
        "win_rate",
        "sharpe_decay",
        "annual_return",
        "annual_volatility",
        "total_return",
        "failures",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for item in cases:
            full = item.get("full", {})
            wf = item.get("wf", {})
            writer.writerow(
                {
                    "label": item.get("label"),
                    "candidate": item.get("candidate"),
                    "leading_passes": item.get("leading_passes"),
                    "sharpe_ratio": full.get("sharpe_ratio"),
                    "avg_oos_sharpe": wf.get("avg_oos_sharpe"),
                    "min_oos_sharpe": _fold_min(wf, "oos"),
                    "max_drawdown": full.get("max_drawdown"),
                    "win_rate": full.get("win_rate"),
                    "sharpe_decay": wf.get("sharpe_decay"),
                    "annual_return": full.get("annual_return"),
                    "annual_volatility": full.get("annual_volatility"),
                    "total_return": full.get("total_return"),
                    "failures": ";".join(item.get("leading_failures", [])),
                }
            )


def main() -> None:
    args = _parse_args()
    started = time.perf_counter()
    candidate_configs = {item["name"]: item for item in _portfolio_configs()}
    if args.candidate not in candidate_configs:
        raise RuntimeError(f"unknown V29 candidate: {args.candidate}")
    base_config = copy.deepcopy(candidate_configs[args.candidate])
    required_sleeves = list(base_config["sleeves"])
    universe_path = Path(args.universe)
    trading_status_path = Path(args.trading_status_path) if str(args.trading_status_path).strip() else None

    sleeve_frame, crisis_returns, carry_returns, close_index, regime, state = _load_research_state(
        universe_path=universe_path,
        min_history=args.min_history,
        start_date=args.start_date,
        end_date=args.end_date,
        required_sleeves=required_sleeves,
        trading_status_path=trading_status_path,
        require_trading_status=bool(args.require_trading_status),
    )

    cases: list[dict[str, Any]] = []
    base_case = _run_case("base", base_config, sleeve_frame, crisis_returns, carry_returns, close_index, regime)
    cases.append(base_case)
    base_returns = _run_meta_portfolio(base_config, sleeve_frame, crisis_returns, carry_returns, close_index, regime)["returns"]

    for key in ["base_gross", "bull_gross", "neutral_gross", "risk_off_gross", "vol_target"]:
        for mult in [0.90, 1.10]:
            config = copy.deepcopy(base_config)
            config[key] = float(config[key]) * mult
            cases.append(_run_case(f"param_{key}_{mult:.2f}x", config, sleeve_frame, crisis_returns, carry_returns, close_index, regime))

    for carry_fraction in [0.04, 0.06, 0.08, 0.10, 0.12]:
        config = copy.deepcopy(base_config)
        config["carry_fraction"] = carry_fraction
        config["crisis_fraction"] = 1.0 - carry_fraction
        cases.append(_run_case(f"carry_fraction_{carry_fraction:.2f}", config, sleeve_frame, crisis_returns, carry_returns, close_index, regime))

    for meta_cost_bps in [5, 10, 20]:
        config = copy.deepcopy(base_config)
        config["meta_cost_bps"] = meta_cost_bps
        cases.append(_run_case(f"meta_cost_bps_{meta_cost_bps}", config, sleeve_frame, crisis_returns, carry_returns, close_index, regime))

    cost_stress_cases: list[dict[str, Any]] = []
    for multiplier in [2.0, 3.0]:
        stressed_sleeve_frame, stressed_crisis, stressed_carry, stressed_index, stressed_regime, _ = _load_research_state(
            universe_path=universe_path,
            min_history=args.min_history,
            start_date=args.start_date,
            end_date=args.end_date,
            required_sleeves=required_sleeves,
            cost_multiplier=multiplier,
            trading_status_path=trading_status_path,
            require_trading_status=bool(args.require_trading_status),
        )
        item = _run_case(
            f"stock_sleeve_cost_{multiplier:.1f}x",
            base_config,
            stressed_sleeve_frame,
            stressed_crisis,
            stressed_carry,
            stressed_index,
            stressed_regime,
        )
        cost_stress_cases.append(item)
        cases.append(item)

    sensitivity_cases = [item for item in cases if item["label"].startswith("param_") or item["label"].startswith("carry_")]
    recent = _recent_metrics(base_returns)
    min_sensitivity_sharpe = min(float(item["full"].get("sharpe_ratio", -999)) for item in sensitivity_cases)
    min_sensitivity_oos = min(float(item["wf"].get("avg_oos_sharpe", -999)) for item in sensitivity_cases)
    min_sensitivity_mdd = min(float(item["full"].get("max_drawdown", -999)) for item in sensitivity_cases)
    min_cost_sharpe = min(float(item["full"].get("sharpe_ratio", -999)) for item in cost_stress_cases)
    min_cost_oos = min(float(item["wf"].get("avg_oos_sharpe", -999)) for item in cost_stress_cases)
    recent_90_sharpe = float(recent["last_90"].get("sharpe_ratio", -999))
    recent_252_sharpe = float(recent["last_252"].get("sharpe_ratio", -999))

    robustness_failures: list[str] = []
    base_failures = base_case.get("leading_failures", [])
    if base_failures:
        robustness_failures.append(f"base leading gate failed: {base_failures}")
    if min_sensitivity_sharpe < ROBUST_THRESHOLDS["min_sensitivity_full_sharpe"]:
        robustness_failures.append(f"sensitivity full Sharpe {min_sensitivity_sharpe:.4f} below threshold")
    if min_sensitivity_oos < ROBUST_THRESHOLDS["min_sensitivity_oos_sharpe"]:
        robustness_failures.append(f"sensitivity OOS Sharpe {min_sensitivity_oos:.4f} below threshold")
    if min_sensitivity_mdd < ROBUST_THRESHOLDS["min_sensitivity_drawdown"]:
        robustness_failures.append(f"sensitivity MDD {min_sensitivity_mdd:.4f} below threshold")
    if min_cost_sharpe < ROBUST_THRESHOLDS["min_cost_stress_full_sharpe"]:
        robustness_failures.append(f"cost-stress full Sharpe {min_cost_sharpe:.4f} below threshold")
    if min_cost_oos < ROBUST_THRESHOLDS["min_cost_stress_oos_sharpe"]:
        robustness_failures.append(f"cost-stress OOS Sharpe {min_cost_oos:.4f} below threshold")
    if recent_90_sharpe < ROBUST_THRESHOLDS["min_recent_90_sharpe"]:
        robustness_failures.append(f"recent 90-day Sharpe {recent_90_sharpe:.4f} below threshold")
    if recent_252_sharpe < ROBUST_THRESHOLDS["min_recent_252_sharpe"]:
        robustness_failures.append(f"recent 252-day Sharpe {recent_252_sharpe:.4f} below threshold")

    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V29-robustness-gate-v31-execution-constrained"
        if state["trading_constraint_summary"].get("enabled")
        else "V29-robustness-gate",
        "research_only": True,
        "production_ready": False,
        "candidate": args.candidate,
        "thresholds": {
            "leading": LEADING_THRESHOLDS,
            "robust": ROBUST_THRESHOLDS,
        },
        "robustness_gate_passes": not robustness_failures,
        "robustness_failures": robustness_failures,
        "summary": {
            "case_count": len(cases),
            "base_full_sharpe": base_case["full"].get("sharpe_ratio"),
            "base_oos_sharpe": base_case["wf"].get("avg_oos_sharpe"),
            "base_max_drawdown": base_case["full"].get("max_drawdown"),
            "base_min_oos_sharpe": _fold_min(base_case["wf"], "oos"),
            "min_sensitivity_full_sharpe": round(min_sensitivity_sharpe, 4),
            "min_sensitivity_oos_sharpe": round(min_sensitivity_oos, 4),
            "min_sensitivity_drawdown": round(min_sensitivity_mdd, 4),
            "min_cost_stress_full_sharpe": round(min_cost_sharpe, 4),
            "min_cost_stress_oos_sharpe": round(min_cost_oos, 4),
            "recent_90_sharpe": recent_90_sharpe,
            "recent_252_sharpe": recent_252_sharpe,
        },
        "recent_metrics": recent,
        "coverage": state["coverage"],
        "trading_constraint_summary": state["trading_constraint_summary"],
        "sleeve_diagnostics": state["sleeve_diagnostics"],
        "cases": cases,
        "production_blockers": [
            "robustness is local research evidence only, not production approval",
            "external WORM/provider entitlement/secret/approval/broker/paper evidence remains required",
        ],
        "elapsed_seconds": round(time.perf_counter() - started, 2),
    }
    output_json = Path(args.output_json)
    output_csv = Path(args.output_csv)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(output_csv, cases)
    print(f"Wrote {output_json}")
    print(f"Wrote {output_csv}")
    print(
        "Robustness gate: "
        f"passes={payload['robustness_gate_passes']} "
        f"base_sharpe={payload['summary']['base_full_sharpe']} "
        f"min_sensitivity_sharpe={payload['summary']['min_sensitivity_full_sharpe']} "
        f"recent_90_sharpe={payload['summary']['recent_90_sharpe']}"
    )
    if robustness_failures:
        print("Failures:")
        for failure in robustness_failures:
            print(f"- {failure}")


if __name__ == "__main__":
    main()
