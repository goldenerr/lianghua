#!/usr/bin/env python3
"""Research V37: executable stock alpha inside the V36 risk layer.

V37 partitions a small RMB account into a V36 ETF sub-account and a much
smaller V32/V29-derived stock-alpha sub-account. The stock sleeve uses the V33
100-share lot, minimum commission, stamp duty and V31 trading-status model.
Only lagged regime, return, volatility, VaR and account drawdown information may
change the stock allocation. Results are research-only and never approve live
trading.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
from _paths import HEDGE_ASSETS_DIR, PROJECT_DIR, RESULTS_DIR
from research_v28_factor_direction import (
    DEFAULT_UNIVERSE,
    _build_factors,
    _build_market_regime_features,
    _load_industry_map,
    _load_market_panel,
    _read_codes,
    _rolling_ic_weights,
)
from research_v29_portfolio_layer import (
    DEFAULT_V31_TRADING_STATUS,
    _load_trading_status_constraints,
    _walk_forward_from_returns,
)
from research_v33_small_account_execution import (
    CostConfig,
    ScenarioConfig,
    _metrics,
)
from research_v33_small_account_execution import (
    _run_scenario as _run_stock_scenario,
)
from research_v36_multi_asset_regime_budget import (
    RISK_PROFILES,
    RegimeBudgetScenario,
    _coverage_summary,
    _load_multi_asset_panel,
    _market_regime,
)
from research_v36_multi_asset_regime_budget import (
    _run_scenario as _run_etf_scenario,
)

DEFAULT_BENCHMARK_DIR = PROJECT_DIR / "data" / "benchmarks"
DEFAULT_V29_SOURCE = (
    RESULTS_DIR / "quant_logic_research_v29_portfolio_layer_20y_v31_execution_constrained.json"
)
DEFAULT_OUTPUT_JSON = RESULTS_DIR / "quant_v37_stock_alpha_multi_asset_budget.json"
DEFAULT_OUTPUT_CSV = RESULTS_DIR / "quant_v37_stock_alpha_multi_asset_budget.csv"
REGIME_ALPHA_MULTIPLIERS = {
    "risk_on": 1.00,
    "neutral": 0.65,
    "cautious": 0.25,
    "risk_off": 0.00,
}
RISK_CAPACITY_BUFFER = 0.01


@dataclass(frozen=True)
class HybridScenario:
    capital: float
    alpha_cap: float
    stock_profile: Literal["v32_guard", "small_balanced"]
    stock_positions: int
    stock_rebalance_freq: int
    stock_rank_mode: Literal["blend"] = "blend"
    overlay_rebalance_freq: int = 20
    alpha_lookback: int = 60


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-dir", default=str(DEFAULT_BENCHMARK_DIR))
    parser.add_argument("--hedge-asset-dir", default=str(HEDGE_ASSETS_DIR))
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    parser.add_argument("--trading-status-path", default=str(DEFAULT_V31_TRADING_STATUS))
    parser.add_argument("--v29-source-json", default=str(DEFAULT_V29_SOURCE))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument("--capitals", default="50000,100000,200000")
    parser.add_argument("--alpha-caps", default="0.10,0.15,0.20")
    parser.add_argument("--stock-profiles", default="v32_guard,small_balanced")
    parser.add_argument("--stock-positions", default="1,2")
    parser.add_argument("--stock-rebalance-freqs", default="20,40")
    parser.add_argument("--overlay-rebalance-freq", type=int, default=20)
    parser.add_argument("--alpha-lookback", type=int, default=60)
    parser.add_argument("--start-date", default="20130101")
    parser.add_argument("--end-date", default="20260529")
    parser.add_argument("--long-history-min-days", type=int, default=1800)
    parser.add_argument("--overlay-cost-bps", type=float, default=5.0)
    parser.add_argument("--commission-rate", type=float, default=0.00025)
    parser.add_argument("--min-commission", type=float, default=5.0)
    parser.add_argument("--stock-stamp-duty-rate", type=float, default=0.0005)
    parser.add_argument("--stock-slippage-bps", type=float, default=5.0)
    parser.add_argument("--etf-slippage-bps", type=float, default=3.0)
    return parser.parse_args()


def _parse_float_list(raw: str) -> list[float]:
    values = [float(value.strip()) for value in raw.split(",") if value.strip()]
    if not values:
        raise ValueError("expected at least one numeric value")
    return values


def _parse_int_list(raw: str) -> list[int]:
    values = [int(value.strip()) for value in raw.split(",") if value.strip()]
    if not values:
        raise ValueError("expected at least one integer value")
    return values


def _parse_str_list(raw: str) -> list[str]:
    values = [value.strip() for value in raw.split(",") if value.strip()]
    if not values:
        raise ValueError("expected at least one string value")
    return values


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_v29_source(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"V29 source evidence is required: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    candidate = next(
        (
            row
            for row in payload.get("results", [])
            if row.get("name") == "v29_price_meta_longhorizon_guard"
        ),
        None,
    )
    if candidate is None:
        raise RuntimeError("V29 long-horizon candidate is missing from source evidence")
    return {
        "path": str(path),
        "sha256": _sha256_file(path),
        "candidate": candidate["name"],
        "config": candidate.get("config", {}),
        "full": candidate.get("full", {}),
        "wf": candidate.get("wf", {}),
        "limitation": (
            "V29 is an idealized broad stock portfolio; V37 production-comparable conclusions use "
            "the V33 executable small-account approximation instead"
        ),
    }


def _build_regime_series(
    close: pd.DataFrame,
    roles: dict[str, str],
    dates: pd.DatetimeIndex,
) -> pd.Series:
    values: list[str] = []
    source_index = pd.DatetimeIndex(close.index)
    for date in dates:
        signal_idx = int(source_index.searchsorted(pd.Timestamp(date), side="left")) - 1
        if signal_idx < 0:
            values.append("risk_off")
            continue
        regime, _diagnostics = _market_regime(close, signal_idx, roles)
        values.append(regime)
    return pd.Series(values, index=dates, dtype="object", name="v36_regime")


def _historical_risk_scale(
    returns: pd.Series,
    *,
    target_vol: float,
    daily_var_limit: float,
) -> tuple[float, float, float]:
    clean = pd.to_numeric(returns, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if len(clean) < 40:
        return 0.0, 0.0, 0.0
    realized_vol = float(clean.std(ddof=1) * np.sqrt(252))
    daily_var95 = float(max(0.0, -clean.quantile(0.05)))
    if not math.isfinite(realized_vol) or realized_vol <= 1e-12:
        return 0.0, realized_vol, daily_var95
    vol_scale = min(1.0, target_vol / realized_vol)
    var_scale = 1.0 if daily_var95 <= 1e-12 else min(1.0, daily_var_limit / daily_var95)
    return max(0.0, min(vol_scale, var_scale)), realized_vol, daily_var95


def _combine_sleeves(
    *,
    etf_returns: pd.Series,
    etf_risky_exposure: pd.Series,
    stock_returns: pd.Series,
    regimes: pd.Series,
    alpha_cap: float,
    overlay_rebalance_freq: int,
    alpha_lookback: int,
    overlay_cost_bps: float,
) -> tuple[pd.Series, pd.Series, dict[str, Any]]:
    if not 0.0 < alpha_cap < 0.50:
        raise ValueError("alpha_cap must be between 0 and 0.50")
    if overlay_rebalance_freq <= 0 or alpha_lookback < 40:
        raise ValueError("invalid overlay timing parameters")
    index = (
        pd.DatetimeIndex(etf_returns.index)
        .intersection(pd.DatetimeIndex(stock_returns.index))
        .sort_values()
    )
    if len(index) < alpha_lookback + 2:
        raise RuntimeError("not enough overlapping sleeve returns")
    etf = pd.to_numeric(etf_returns.reindex(index), errors="coerce").fillna(0.0)
    etf_exposure = (
        pd.to_numeric(etf_risky_exposure.reindex(index), errors="coerce")
        .ffill()
        .fillna(0.0)
        .clip(lower=0.0, upper=1.0)
    )
    stock = pd.to_numeric(stock_returns.reindex(index), errors="coerce").fillna(0.0)
    regime_series = regimes.reindex(index).fillna("risk_off")
    profile = RISK_PROFILES["aggressive"]
    allocation = 0.0
    previous_allocation = 0.0
    equity = 1.0
    peak_equity = 1.0
    combined_values: list[float] = []
    allocation_values: list[float] = []
    allocation_turnover = 0.0
    overlay_cost_total = 0.0
    risk_off_rebalances = 0
    momentum_blocked_rebalances = 0
    drawdown_reduced_rebalances = 0
    realized_vol_values: list[float] = []
    daily_var_values: list[float] = []
    estimated_total_exposures: list[float] = []
    cost_rate = overlay_cost_bps / 10000.0

    for i, _date in enumerate(index):
        desired_allocation = allocation
        if i % overlay_rebalance_freq == 0:
            target = 0.0
            if i >= alpha_lookback:
                regime = str(regime_series.iloc[i])
                regime_scale = float(REGIME_ALPHA_MULTIPLIERS.get(regime, 0.0))
                risk_off_rebalances += int(regime == "risk_off")
                stock_history = stock.iloc[i - alpha_lookback : i]
                alpha_trend = float((1.0 + stock_history).prod() - 1.0)
                trend_scale = 1.0 if alpha_trend > 0.0 else 0.0
                momentum_blocked_rebalances += int(trend_scale == 0.0)
                target = alpha_cap * regime_scale * trend_scale
                candidate_history = etf.iloc[i - alpha_lookback : i] + target * stock_history
                risk_scale, realized_vol, daily_var95 = _historical_risk_scale(
                    candidate_history,
                    target_vol=float(profile["target_vol"]),
                    daily_var_limit=float(profile["daily_var_limit"]),
                )
                realized_vol_values.append(realized_vol)
                daily_var_values.append(daily_var95)
                target *= risk_scale
                account_drawdown = equity / peak_equity - 1.0 if peak_equity > 0 else 0.0
                if account_drawdown <= -0.08:
                    target = 0.0
                    drawdown_reduced_rebalances += 1
                elif account_drawdown <= -0.05:
                    target *= 0.25
                    drawdown_reduced_rebalances += 1
            desired_allocation = max(0.0, min(alpha_cap, float(target)))

        prior_etf_exposure = (
            float(etf_exposure.iloc[i - 1]) if i > 0 else float(etf_exposure.iloc[i])
        )
        risk_capacity_limit = max(0.0, float(profile["max_budget"]) - RISK_CAPACITY_BUFFER)
        risk_headroom = max(0.0, risk_capacity_limit - prior_etf_exposure)
        desired_allocation = min(desired_allocation, risk_headroom)
        allocation_change = abs(desired_allocation - previous_allocation)
        rebalance_cost = allocation_change * cost_rate
        if allocation_change > 1e-12:
            allocation_turnover += allocation_change
            overlay_cost_total += rebalance_cost
            allocation = desired_allocation
            previous_allocation = allocation

        daily_return = float(etf.iloc[i]) + allocation * float(stock.iloc[i]) - rebalance_cost
        daily_return = max(-0.99, daily_return)
        equity *= 1.0 + daily_return
        peak_equity = max(peak_equity, equity)
        combined_values.append(float(daily_return))
        allocation_values.append(float(allocation))
        estimated_total_exposures.append(float(etf_exposure.iloc[i]) + float(allocation))

    combined = pd.Series(combined_values, index=index, dtype=float, name="v37_combined")
    weights = pd.Series(allocation_values, index=index, dtype=float, name="stock_alpha_allocation")
    diagnostics = {
        "etf_capital_weight": 1.0,
        "alpha_cap": round(alpha_cap, 4),
        "avg_alpha_allocation": round(float(weights.mean()), 4),
        "max_alpha_allocation": round(float(weights.max()), 4),
        "active_alpha_days": int((weights > 1e-12).sum()),
        "active_alpha_ratio": round(float((weights > 1e-12).mean()), 4),
        "allocation_turnover": round(float(allocation_turnover), 4),
        "overlay_cost_total_return": round(float(overlay_cost_total), 6),
        "risk_off_rebalances": int(risk_off_rebalances),
        "momentum_blocked_rebalances": int(momentum_blocked_rebalances),
        "drawdown_reduced_rebalances": int(drawdown_reduced_rebalances),
        "avg_realized_vol_at_rebalance": round(float(np.mean(realized_vol_values)), 4)
        if realized_vol_values
        else 0.0,
        "avg_daily_var95_at_rebalance": round(float(np.mean(daily_var_values)), 4)
        if daily_var_values
        else 0.0,
        "avg_estimated_total_risk_exposure": round(float(np.mean(estimated_total_exposures)), 4),
        "max_estimated_total_risk_exposure": round(float(max(estimated_total_exposures)), 4),
        "risk_capacity_limit": round(float(profile["max_budget"]), 4),
        "risk_capacity_buffer": round(RISK_CAPACITY_BUFFER, 4),
    }
    return combined, weights, diagnostics


def _metrics_for_returns(returns: pd.Series, capital: float) -> dict[str, Any]:
    final_nav = float(capital * (1.0 + returns).prod())
    return _metrics(returns, final_nav=final_nav, initial_capital=capital)


def _min_oos(wf: dict[str, Any]) -> float | None:
    values = [float(fold["oos"]) for fold in wf.get("folds", []) if fold.get("oos") is not None]
    return round(min(values), 4) if values else None


def _evaluate_gates(
    *,
    full: dict[str, Any],
    wf: dict[str, Any],
    baseline_full: dict[str, Any],
    baseline_wf: dict[str, Any],
    stock_full: dict[str, Any],
    allocation_diagnostics: dict[str, Any],
) -> dict[str, Any]:
    annual_return_delta = float(full.get("annual_return", -999)) - float(
        baseline_full.get("annual_return", -999)
    )
    sharpe_delta = float(full.get("sharpe_ratio", -999)) - float(
        baseline_full.get("sharpe_ratio", -999)
    )
    drawdown_delta = float(full.get("max_drawdown", -999)) - float(
        baseline_full.get("max_drawdown", -999)
    )
    oos_delta = float(wf.get("avg_oos_sharpe", -999)) - float(
        baseline_wf.get("avg_oos_sharpe", -999)
    )
    min_oos = _min_oos(wf)
    baseline_min_oos = _min_oos(baseline_wf)
    min_oos_delta = (
        float(min_oos) - float(baseline_min_oos)
        if min_oos is not None and baseline_min_oos is not None
        else -999.0
    )
    return_improved = annual_return_delta >= 0.005
    sharpe_not_degraded = sharpe_delta >= 0.0
    drawdown_controlled = float(full.get("max_drawdown", -999)) >= -0.15 and drawdown_delta >= -0.02
    oos_not_degraded = oos_delta >= 0.0
    min_oos_not_degraded = min_oos_delta >= 0.0
    risk_capacity_controlled = (
        float(allocation_diagnostics.get("max_estimated_total_risk_exposure", 999.0)) <= 0.95
    )
    executable = (
        float(stock_full.get("avg_holdings", 0.0)) >= 0.25
        and int(stock_full.get("orders", 0)) > 0
        and int(stock_full.get("cash_blocked_buys", 10**9))
        <= int(stock_full.get("orders", 0)) * 0.25 + 10
    )
    minimum_gate = (
        float(full.get("sharpe_ratio", -999)) >= 1.2
        and float(full.get("max_drawdown", -999)) >= -0.15
        and float(full.get("win_rate", 0.0)) >= 0.40
    )
    simultaneous_improvement = (
        return_improved
        and sharpe_not_degraded
        and drawdown_controlled
        and oos_not_degraded
        and min_oos_not_degraded
        and risk_capacity_controlled
        and executable
    )
    return {
        "annual_return_delta": round(annual_return_delta, 4),
        "sharpe_delta": round(sharpe_delta, 4),
        "max_drawdown_delta": round(drawdown_delta, 4),
        "avg_oos_sharpe_delta": round(oos_delta, 4),
        "min_oos_sharpe_delta": round(min_oos_delta, 4),
        "return_improved_50bps": bool(return_improved),
        "sharpe_not_degraded": bool(sharpe_not_degraded),
        "drawdown_controlled": bool(drawdown_controlled),
        "oos_not_degraded": bool(oos_not_degraded),
        "min_oos_not_degraded": bool(min_oos_not_degraded),
        "risk_capacity_controlled": bool(risk_capacity_controlled),
        "stock_execution_gate_passes": bool(executable),
        "small_account_minimum_gate_passes": bool(minimum_gate),
        "simultaneous_improvement_gate_passes": bool(simultaneous_improvement),
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "name",
        "capital",
        "alpha_cap",
        "stock_profile",
        "stock_positions",
        "stock_rebalance_freq",
        "simultaneous_improvement_gate_passes",
        "stock_execution_gate_passes",
        "small_account_minimum_gate_passes",
        "sharpe_ratio",
        "annual_return",
        "max_drawdown",
        "win_rate",
        "avg_oos_sharpe",
        "min_oos_sharpe",
        "annual_return_delta",
        "sharpe_delta",
        "max_drawdown_delta",
        "avg_oos_sharpe_delta",
        "min_oos_sharpe_delta",
        "avg_alpha_allocation",
        "active_alpha_ratio",
        "stock_subaccount_capital",
        "stock_avg_holdings",
        "stock_orders",
        "stock_total_fees",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            scenario = row["scenario"]
            full = row["full"]
            gates = row["gates"]
            stock = row["stock_subaccount"]
            writer.writerow(
                {
                    "name": row["name"],
                    "capital": scenario["capital"],
                    "alpha_cap": scenario["alpha_cap"],
                    "stock_profile": scenario["stock_profile"],
                    "stock_positions": scenario["stock_positions"],
                    "stock_rebalance_freq": scenario["stock_rebalance_freq"],
                    "simultaneous_improvement_gate_passes": gates[
                        "simultaneous_improvement_gate_passes"
                    ],
                    "stock_execution_gate_passes": gates["stock_execution_gate_passes"],
                    "small_account_minimum_gate_passes": gates["small_account_minimum_gate_passes"],
                    "sharpe_ratio": full.get("sharpe_ratio"),
                    "annual_return": full.get("annual_return"),
                    "max_drawdown": full.get("max_drawdown"),
                    "win_rate": full.get("win_rate"),
                    "avg_oos_sharpe": row["wf"].get("avg_oos_sharpe"),
                    "min_oos_sharpe": _min_oos(row["wf"]),
                    "annual_return_delta": gates["annual_return_delta"],
                    "sharpe_delta": gates["sharpe_delta"],
                    "max_drawdown_delta": gates["max_drawdown_delta"],
                    "avg_oos_sharpe_delta": gates["avg_oos_sharpe_delta"],
                    "min_oos_sharpe_delta": gates["min_oos_sharpe_delta"],
                    "avg_alpha_allocation": row["allocation_diagnostics"]["avg_alpha_allocation"],
                    "active_alpha_ratio": row["allocation_diagnostics"]["active_alpha_ratio"],
                    "stock_subaccount_capital": stock["capital"],
                    "stock_avg_holdings": stock["full"].get("avg_holdings"),
                    "stock_orders": stock["full"].get("orders"),
                    "stock_total_fees": stock["full"].get("total_fees"),
                }
            )


def main() -> None:
    started = time.time()
    args = _parse_args()
    capitals = _parse_float_list(args.capitals)
    alpha_caps = _parse_float_list(args.alpha_caps)
    stock_profiles = _parse_str_list(args.stock_profiles)
    stock_positions = _parse_int_list(args.stock_positions)
    stock_rebalance_freqs = _parse_int_list(args.stock_rebalance_freqs)
    if any(cap <= 0.0 or cap >= 0.50 for cap in alpha_caps):
        raise ValueError("alpha caps must be between 0 and 0.50")
    if set(stock_profiles) - {"v32_guard", "small_balanced"}:
        raise ValueError("unsupported stock profile")
    if any(value <= 0 for value in stock_positions + stock_rebalance_freqs):
        raise ValueError("stock positions and rebalance frequencies must be positive")

    benchmark_dir = Path(args.benchmark_dir)
    hedge_asset_dir = Path(args.hedge_asset_dir)
    universe_path = Path(args.universe)
    trading_status_path = Path(args.trading_status_path)
    v29_source = _load_v29_source(Path(args.v29_source_json))
    output_json = Path(args.output_json)
    output_csv = Path(args.output_csv)

    etf_close, etf_metadata, etf_roles = _load_multi_asset_panel(
        benchmark_dir,
        hedge_asset_dir,
        "core_long",
        long_history_min_days=int(args.long_history_min_days),
    )
    etf_close = etf_close.loc[pd.Timestamp(args.start_date) : pd.Timestamp(args.end_date)].dropna(
        how="all"
    )
    etf_coverage = _coverage_summary(
        etf_metadata, long_history_min_days=int(args.long_history_min_days)
    )

    codes = _read_codes(universe_path)
    stock_close, stock_volume, stock_amount = _load_market_panel(codes, min_history=252)
    stock_close = stock_close.loc[
        pd.Timestamp(args.start_date) : pd.Timestamp(args.end_date)
    ].copy()
    stock_volume = stock_volume.reindex(stock_close.index)
    stock_amount = stock_amount.reindex(stock_close.index)
    if len(stock_close) < 600:
        raise RuntimeError("not enough stock data for V37")
    valuation_close = stock_close.ffill()
    factors = _build_factors(stock_close, stock_volume, stock_amount)
    future_5d = stock_close.pct_change(5, fill_method=None).shift(-5)
    rolling_ic = _rolling_ic_weights(factors, future_5d)
    stock_regime = _build_market_regime_features(stock_close)
    industry_map = _load_industry_map()
    trading_constraints, trading_summary = _load_trading_status_constraints(
        trading_status_path,
        stock_close.index,
        stock_close.columns,
    )
    if trading_constraints is None:
        raise RuntimeError("V37 requires V31 trading-status constraints")

    stock_cost = CostConfig(
        commission_rate=float(args.commission_rate),
        min_commission=float(args.min_commission),
        stamp_duty_rate=float(args.stock_stamp_duty_rate),
        slippage_bps=float(args.stock_slippage_bps),
    )
    etf_cost = CostConfig(
        commission_rate=float(args.commission_rate),
        min_commission=float(args.min_commission),
        stamp_duty_rate=0.0,
        slippage_bps=float(args.etf_slippage_bps),
    )

    baseline_cache: dict[float, tuple[pd.Series, pd.Series, dict[str, Any]]] = {}
    regime_cache: dict[tuple[pd.Timestamp, pd.Timestamp, int], pd.Series] = {}
    for capital in capitals:
        baseline_scenario = RegimeBudgetScenario(
            capital=capital,
            universe_mode="core_long",
            max_assets=1,
            rebalance_freq=20,
            rank_mode="risk_adjusted",
            risk_profile="aggressive",
        )
        baseline_result = _run_etf_scenario(
            etf_close,
            baseline_scenario,
            etf_cost,
            etf_roles,
            etf_coverage,
            include_return_series=True,
        )
        baseline_series = baseline_result.pop("_return_series")
        baseline_exposure = baseline_result.pop("_risky_exposure_series")
        baseline_cache[capital] = (baseline_series, baseline_exposure, baseline_result)

    rows: list[dict[str, Any]] = []
    for capital in capitals:
        baseline_series, baseline_exposure, etf_result = baseline_cache[capital]
        for alpha_cap in alpha_caps:
            for profile in stock_profiles:
                for position_count in stock_positions:
                    for stock_freq in stock_rebalance_freqs:
                        stock_subaccount_capital = capital * alpha_cap
                        stock_scenario = ScenarioConfig(
                            capital=stock_subaccount_capital,
                            max_positions=position_count,
                            rank_mode="blend",
                            gross_profile=profile,  # type: ignore[arg-type]
                            rebalance_freq=stock_freq,
                            max_per_industry=1,
                            candidate_pool_multiplier=10,
                            per_position_budget_buffer=1.50,
                        )
                        stock_result = _run_stock_scenario(
                            scenario=stock_scenario,
                            close=stock_close,
                            valuation_close=valuation_close,
                            factors=factors,
                            rolling_ic=rolling_ic,
                            regime=stock_regime,
                            industry_map=industry_map,
                            trading_constraints=trading_constraints,
                            cost=stock_cost,
                            include_return_series=True,
                        )
                        stock_series = stock_result.pop("_return_series")

                        common_index = (
                            pd.DatetimeIndex(baseline_series.index)
                            .intersection(pd.DatetimeIndex(stock_series.index))
                            .sort_values()
                        )
                        regime_key = (common_index[0], common_index[-1], len(common_index))
                        regimes = regime_cache.get(regime_key)
                        if regimes is None or not regimes.index.equals(common_index):
                            regimes = _build_regime_series(etf_close, etf_roles, common_index)
                            regime_cache[regime_key] = regimes
                        combined, _alpha_weights, allocation_diag = _combine_sleeves(
                            etf_returns=baseline_series.reindex(common_index),
                            etf_risky_exposure=baseline_exposure.reindex(common_index),
                            stock_returns=stock_series.reindex(common_index),
                            regimes=regimes,
                            alpha_cap=alpha_cap,
                            overlay_rebalance_freq=int(args.overlay_rebalance_freq),
                            alpha_lookback=int(args.alpha_lookback),
                            overlay_cost_bps=float(args.overlay_cost_bps),
                        )
                        baseline_aligned = baseline_series.reindex(combined.index).fillna(0.0)
                        full = _metrics_for_returns(combined, capital)
                        wf = _walk_forward_from_returns(combined) if len(combined) > 2100 else {}
                        baseline_full = _metrics_for_returns(baseline_aligned, capital)
                        baseline_wf = (
                            _walk_forward_from_returns(baseline_aligned)
                            if len(baseline_aligned) > 2100
                            else {}
                        )
                        gates = _evaluate_gates(
                            full=full,
                            wf=wf,
                            baseline_full=baseline_full,
                            baseline_wf=baseline_wf,
                            stock_full=stock_result["full"],
                            allocation_diagnostics=allocation_diag,
                        )
                        scenario = HybridScenario(
                            capital=capital,
                            alpha_cap=alpha_cap,
                            stock_profile=profile,  # type: ignore[arg-type]
                            stock_positions=position_count,
                            stock_rebalance_freq=stock_freq,
                            overlay_rebalance_freq=int(args.overlay_rebalance_freq),
                            alpha_lookback=int(args.alpha_lookback),
                        )
                        rows.append(
                            {
                                "name": (
                                    f"v37_{int(capital)}_alpha{int(alpha_cap * 100)}_"
                                    f"{profile}_{position_count}pos_{stock_freq}d"
                                ),
                                "scenario": asdict(scenario),
                                "research_only": True,
                                "production_ready": False,
                                "full": full,
                                "wf": wf,
                                "baseline_full": baseline_full,
                                "baseline_wf": baseline_wf,
                                "gates": gates,
                                "allocation_diagnostics": allocation_diag,
                                "etf_subaccount": {
                                    "capital": round(capital, 2),
                                    "full": etf_result["full"],
                                },
                                "stock_subaccount": {
                                    "capital": round(stock_subaccount_capital, 2),
                                    "scenario": stock_result["scenario"],
                                    "full": stock_result["full"],
                                    "wf": stock_result["wf"],
                                },
                            }
                        )

    rows.sort(
        key=lambda row: (
            bool(row["gates"]["simultaneous_improvement_gate_passes"]),
            bool(row["gates"]["stock_execution_gate_passes"]),
            float(row["gates"]["annual_return_delta"]),
            float(row["full"].get("sharpe_ratio", -999)),
        ),
        reverse=True,
    )
    best_by_capital: dict[str, Any] = {}
    for capital in capitals:
        candidates = [row for row in rows if float(row["scenario"]["capital"]) == capital]
        if candidates:
            best_by_capital[str(int(capital))] = candidates[0]

    report = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V37-stock-alpha-multi-asset-budget-v1",
        "research_only": True,
        "production_ready": False,
        "objective": "inject executable V32/V29-derived stock alpha into the V36 small-account risk layer",
        "methodology": {
            "capital_partition": "stock alpha uses only lagged V36 idle risk headroom; ETF capital is not reduced",
            "signal_timing": "all overlay decisions use prior-day or older data",
            "risk_layer": "V36 regime, 60-day volatility/VaR scale, 5% drawdown reduction and 8% stop",
            "stock_execution": "100-share lots, minimum commission, stamp duty, slippage and V31 status",
            "comparison": "same-date full-capital V36 core-long aggressive risk-adjusted baseline",
        },
        "v29_source_evidence": v29_source,
        "universe": str(universe_path),
        "universe_sha256": _sha256_file(universe_path),
        "stock_coverage": {
            "symbols_loaded": int(stock_close.shape[1]),
            "dates": int(len(stock_close)),
            "start": pd.Timestamp(stock_close.index.min()).date().isoformat(),
            "end": pd.Timestamp(stock_close.index.max()).date().isoformat(),
            "industry_overlap": int(
                sum(1 for symbol in stock_close.columns if symbol in industry_map)
            ),
        },
        "trading_constraint_summary": trading_summary,
        "etf_coverage": etf_coverage,
        "cost_model": {
            "stock": asdict(stock_cost),
            "etf": asdict(etf_cost),
            "overlay_cost_bps": float(args.overlay_cost_bps),
        },
        "scenario_count": int(len(rows)),
        "simultaneous_improvement_pass_count": int(
            sum(1 for row in rows if row["gates"]["simultaneous_improvement_gate_passes"])
        ),
        "stock_execution_pass_count": int(
            sum(1 for row in rows if row["gates"]["stock_execution_gate_passes"])
        ),
        "minimum_gate_pass_count": int(
            sum(1 for row in rows if row["gates"]["small_account_minimum_gate_passes"])
        ),
        "best_by_capital": best_by_capital,
        "results": rows,
        "production_blockers": [
            "V37 is local research evidence and does not approve live trading",
            "V31 trading status is a free-source approximation, not vendor PIT evidence",
            "top-level fractional sleeve scaling approximates sub-account resizing and requires broker replay",
            "requires real ETF/stock provider entitlement, 90-day paper fills and position reconciliation",
            "requires external WORM/Secret/Approval/Position/Capacity/DR evidence gate",
        ],
        "elapsed_seconds": round(time.time() - started, 2),
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(output_csv, rows)
    print(
        json.dumps(
            {
                "production_ready": False,
                "scenario_count": len(rows),
                "simultaneous_improvement_pass_count": report[
                    "simultaneous_improvement_pass_count"
                ],
                "stock_execution_pass_count": report["stock_execution_pass_count"],
                "minimum_gate_pass_count": report["minimum_gate_pass_count"],
                "best_by_capital": {
                    capital: {
                        "name": row["name"],
                        "sharpe": row["full"].get("sharpe_ratio"),
                        "annual_return": row["full"].get("annual_return"),
                        "max_drawdown": row["full"].get("max_drawdown"),
                        "annual_return_delta": row["gates"]["annual_return_delta"],
                        "simultaneous_pass": row["gates"]["simultaneous_improvement_gate_passes"],
                    }
                    for capital, row in best_by_capital.items()
                },
                "output_json": str(output_json),
                "output_csv": str(output_csv),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
