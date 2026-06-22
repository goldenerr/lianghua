#!/usr/bin/env python3
"""Research V35: small-account ETF rotation with volatility/VaR risk budget.

V34 showed ETF rotation is executable for small accounts but still has
unacceptable drawdown. V35 changes the objective from return chasing to risk
budgeting: selected ETFs are scaled by target volatility, historical 95% VaR and
account drawdown guards. This is research-only and never approves live trading.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
from _paths import RESULTS_DIR
from research_v29_portfolio_layer import _walk_forward_from_returns
from research_v33_small_account_execution import CostConfig, _metrics
from research_v34_small_account_etf_rotation import (
    CASH_PROXY,
    DEFAULT_BENCHMARK_DIR,
    DEFAULT_ETF_SYMBOLS,
    _execute_rebalance,
    _load_etf_panel,
    _score_assets,
)

DEFAULT_OUTPUT_JSON = RESULTS_DIR / "quant_v35_small_account_risk_budget.json"
DEFAULT_OUTPUT_CSV = RESULTS_DIR / "quant_v35_small_account_risk_budget.csv"
RISK_PROFILES: dict[str, dict[str, float]] = {
    "balanced_var": {
        "max_risky_budget": 0.50,
        "min_signal": 0.00,
        "dd_reduce_threshold": 0.08,
        "dd_reduce_scale": 0.40,
        "dd_stop_threshold": 0.14,
        "dd_stop_scale": 0.10,
        "min_budget_to_trade": 0.05,
    },
    "conservative_var": {
        "max_risky_budget": 0.35,
        "min_signal": 0.02,
        "dd_reduce_threshold": 0.06,
        "dd_reduce_scale": 0.25,
        "dd_stop_threshold": 0.10,
        "dd_stop_scale": 0.0,
        "min_budget_to_trade": 0.04,
    },
    "ultra_var": {
        "max_risky_budget": 0.25,
        "min_signal": 0.04,
        "dd_reduce_threshold": 0.04,
        "dd_reduce_scale": 0.15,
        "dd_stop_threshold": 0.08,
        "dd_stop_scale": 0.0,
        "min_budget_to_trade": 0.03,
    },
}


@dataclass(frozen=True)
class RiskBudgetScenario:
    capital: float
    max_assets: int
    rebalance_freq: int
    rank_mode: Literal["momentum", "risk_adjusted", "defensive"]
    risk_profile: Literal["balanced_var", "conservative_var", "ultra_var"]
    target_vol: float
    daily_var_limit: float
    lookback: int = 126
    lot_size: int = 100


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-dir", default=str(DEFAULT_BENCHMARK_DIR))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument("--symbols", default=",".join(DEFAULT_ETF_SYMBOLS))
    parser.add_argument("--capitals", default="50000,100000,200000")
    parser.add_argument("--max-assets", default="1,2")
    parser.add_argument("--rebalance-freqs", default="20,40")
    parser.add_argument("--rank-modes", default="momentum,risk_adjusted")
    parser.add_argument("--risk-profiles", default="balanced_var,conservative_var,ultra_var")
    parser.add_argument("--target-vols", default="0.04,0.06,0.08")
    parser.add_argument("--daily-var-limits", default="0.008,0.012,0.016")
    parser.add_argument("--start-date", default="20130101")
    parser.add_argument("--end-date", default="20260529")
    parser.add_argument("--commission-rate", type=float, default=0.00025)
    parser.add_argument("--min-commission", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=3.0)
    return parser.parse_args()


def _parse_float_list(raw: str) -> list[float]:
    values = [float(item.strip()) for item in raw.split(",") if item.strip()]
    if not values:
        raise ValueError("expected at least one numeric value")
    return values


def _parse_int_list(raw: str) -> list[int]:
    values = [int(item.strip()) for item in raw.split(",") if item.strip()]
    if not values:
        raise ValueError("expected at least one integer value")
    return values


def _parse_str_list(raw: str) -> list[str]:
    values = [item.strip() for item in raw.split(",") if item.strip()]
    if not values:
        raise ValueError("expected at least one string value")
    return values


def _risk_symbols(score: pd.Series, scenario: RiskBudgetScenario) -> list[str]:
    profile = RISK_PROFILES[scenario.risk_profile]
    candidates = score.drop(labels=[CASH_PROXY], errors="ignore").replace([np.inf, -np.inf], np.nan).dropna()
    candidates = candidates[candidates >= float(profile["min_signal"])]
    if candidates.empty:
        return []
    return [str(symbol) for symbol in candidates.sort_values(ascending=False).head(scenario.max_assets).index]


def _historical_budget(
    close: pd.DataFrame,
    *,
    signal_idx: int,
    selected: list[str],
    scenario: RiskBudgetScenario,
    nav: float,
    peak_nav: float,
) -> tuple[float, dict[str, float]]:
    profile = RISK_PROFILES[scenario.risk_profile]
    diagnostics = {
        "budget_before_guards": 0.0,
        "realized_vol": 0.0,
        "daily_var95": 0.0,
        "vol_scale": 0.0,
        "var_scale": 0.0,
        "drawdown_scale": 1.0,
        "account_drawdown": 0.0,
    }
    if not selected:
        return 0.0, diagnostics
    start = max(0, signal_idx - scenario.lookback + 1)
    history = close[selected].iloc[start : signal_idx + 1].ffill().pct_change(fill_method=None).dropna(how="any")
    if len(history) < 40:
        return 0.0, diagnostics
    portfolio_returns = history.mean(axis=1)
    realized_vol = float(portfolio_returns.std(ddof=1) * np.sqrt(252))
    daily_var95 = float(max(0.0, -portfolio_returns.quantile(0.05)))
    if not math.isfinite(realized_vol) or realized_vol <= 1e-12:
        return 0.0, diagnostics
    vol_scale = min(1.0, float(scenario.target_vol) / realized_vol)
    var_scale = 1.0 if daily_var95 <= 1e-12 else min(1.0, float(scenario.daily_var_limit) / daily_var95)
    budget = float(profile["max_risky_budget"]) * min(vol_scale, var_scale)
    account_drawdown = nav / peak_nav - 1.0 if peak_nav > 0 else 0.0
    drawdown_scale = 1.0
    if account_drawdown <= -float(profile["dd_stop_threshold"]):
        drawdown_scale = float(profile["dd_stop_scale"])
    elif account_drawdown <= -float(profile["dd_reduce_threshold"]):
        drawdown_scale = float(profile["dd_reduce_scale"])
    diagnostics.update(
        {
            "budget_before_guards": float(budget),
            "realized_vol": realized_vol,
            "daily_var95": daily_var95,
            "vol_scale": float(vol_scale),
            "var_scale": float(var_scale),
            "drawdown_scale": float(drawdown_scale),
            "account_drawdown": float(account_drawdown),
        }
    )
    budget *= drawdown_scale
    if budget < float(profile["min_budget_to_trade"]):
        return 0.0, diagnostics
    return max(0.0, min(float(profile["max_risky_budget"]), float(budget))), diagnostics


def _risk_budget_target_weights(
    close: pd.DataFrame,
    *,
    signal_idx: int,
    score: pd.Series,
    scenario: RiskBudgetScenario,
    nav: float,
    peak_nav: float,
) -> tuple[dict[str, float], dict[str, float]]:
    selected = _risk_symbols(score, scenario)
    budget, diagnostics = _historical_budget(
        close,
        signal_idx=signal_idx,
        selected=selected,
        scenario=scenario,
        nav=nav,
        peak_nav=peak_nav,
    )
    if budget <= 1e-12 or not selected:
        diagnostics["selected_count"] = 0.0
        diagnostics["final_budget"] = 0.0
        return {}, diagnostics
    weight = budget / len(selected)
    diagnostics["selected_count"] = float(len(selected))
    diagnostics["final_budget"] = float(budget)
    return {symbol: float(weight) for symbol in selected}, diagnostics


def _portfolio_value(positions: dict[str, int], cash: float, prices: pd.Series) -> tuple[float, float]:
    asset_value = 0.0
    for symbol, shares in positions.items():
        price = float(prices.get(symbol, np.nan))
        if math.isfinite(price) and price > 0:
            asset_value += float(shares) * price
    return float(cash + asset_value), float(asset_value)


def _run_scenario(close: pd.DataFrame, scenario: RiskBudgetScenario, cost: CostConfig) -> dict[str, Any]:
    valuation_close = close.ffill()
    positions: dict[str, int] = {}
    cash = float(scenario.capital)
    prev_nav = float(scenario.capital)
    peak_nav = float(scenario.capital)
    returns: list[float] = []
    dates: list[pd.Timestamp] = []
    holdings_counts: list[int] = []
    risky_exposures: list[float] = []
    cash_weights: list[float] = []
    budget_values: list[float] = []
    budget_diagnostics: list[dict[str, float]] = []
    order_diagnostics = {
        "orders": 0.0,
        "buy_orders": 0.0,
        "sell_orders": 0.0,
        "buy_notional": 0.0,
        "sell_notional": 0.0,
        "fees": 0.0,
        "lot_blocked_targets": 0.0,
        "cash_blocked_buys": 0.0,
        "missing_price_blocks": 0.0,
    }
    start_idx = max(scenario.lookback + 1, 130)
    for i in range(start_idx, len(close)):
        date = pd.Timestamp(close.index[i])
        nav_before, _asset_value_before = _portfolio_value(positions, cash, valuation_close.iloc[i])
        if nav_before <= 0:
            raise RuntimeError(f"NAV became non-positive on {date.date()}: {nav_before}")
        if (i - start_idx) % scenario.rebalance_freq == 0:
            score = _score_assets(close, i - 1, scenario)
            target_weights, budget_diag = _risk_budget_target_weights(
                close,
                signal_idx=i - 1,
                score=score,
                scenario=scenario,
                nav=nav_before,
                peak_nav=peak_nav,
            )
            positions, cash, diag = _execute_rebalance(
                positions=positions,
                cash=cash,
                target_weights=target_weights,
                nav=nav_before,
                prices=close.iloc[i],
                scenario=scenario,
                cost=cost,
            )
            for key, value in diag.items():
                order_diagnostics[key] += float(value)
            budget_values.append(float(budget_diag.get("final_budget", 0.0)))
            budget_diagnostics.append(budget_diag)

        nav_after, asset_value_after = _portfolio_value(positions, cash, valuation_close.iloc[i])
        returns.append(float(nav_after / prev_nav - 1.0))
        dates.append(date)
        prev_nav = nav_after
        peak_nav = max(peak_nav, nav_after)
        holdings_counts.append(len(positions))
        risky_exposures.append(float(asset_value_after / nav_after) if nav_after > 0 else 0.0)
        cash_weights.append(float(cash / nav_after) if nav_after > 0 else 0.0)

    returns_series = pd.Series(returns, index=pd.DatetimeIndex(dates), dtype=float)
    full = _metrics(returns_series, final_nav=prev_nav, initial_capital=scenario.capital)
    full.update(
        {
            "avg_holdings": round(float(np.mean(holdings_counts)), 2) if holdings_counts else 0.0,
            "max_holdings": int(max(holdings_counts)) if holdings_counts else 0,
            "avg_risky_exposure": round(float(np.mean(risky_exposures)), 4) if risky_exposures else 0.0,
            "avg_cash_weight": round(float(np.mean(cash_weights)), 4) if cash_weights else 0.0,
            "avg_target_budget": round(float(np.mean(budget_values)), 4) if budget_values else 0.0,
            "max_target_budget": round(float(max(budget_values)), 4) if budget_values else 0.0,
            "zero_budget_rebalances": int(sum(1 for value in budget_values if value <= 1e-12)),
            "avg_realized_vol_at_rebalance": round(
                float(np.mean([item["realized_vol"] for item in budget_diagnostics])),
                4,
            )
            if budget_diagnostics
            else 0.0,
            "avg_daily_var95_at_rebalance": round(
                float(np.mean([item["daily_var95"] for item in budget_diagnostics])),
                4,
            )
            if budget_diagnostics
            else 0.0,
            "total_fees": round(float(order_diagnostics["fees"]), 2),
            "fees_pct_initial_capital": round(float(order_diagnostics["fees"] / scenario.capital), 4),
            "turnover_pct_initial_capital": round(
                float((order_diagnostics["buy_notional"] + order_diagnostics["sell_notional"]) / scenario.capital),
                4,
            ),
            "orders": int(order_diagnostics["orders"]),
            "lot_blocked_targets": int(order_diagnostics["lot_blocked_targets"]),
            "cash_blocked_buys": int(order_diagnostics["cash_blocked_buys"]),
            "missing_price_blocks": int(order_diagnostics["missing_price_blocks"]),
        }
    )
    wf = _walk_forward_from_returns(returns_series) if len(returns_series) > 2100 else {}
    minimum_gate = (
        full.get("sharpe_ratio", -999) >= 1.2
        and full.get("max_drawdown", -999) >= -0.15
        and full.get("win_rate", 0.0) >= 0.40
    )
    risk_gate = full.get("max_drawdown", -999) >= -0.15
    executable_gate = (
        full.get("avg_holdings", 0.0) >= 0.10
        and full.get("orders", 0) > 0
        and full.get("cash_blocked_buys", 10**9) <= full.get("orders", 0) * 0.20 + 10
    )
    return {
        "name": (
            f"v35_risk_budget_{scenario.risk_profile}_{scenario.rank_mode}_"
            f"{int(scenario.capital)}_{scenario.max_assets}asset_{scenario.rebalance_freq}d_"
            f"vol{scenario.target_vol:.2f}_var{scenario.daily_var_limit:.3f}"
        ),
        "scenario": asdict(scenario),
        "cost": asdict(cost),
        "research_only": True,
        "production_ready": False,
        "risk_gate_passes": bool(risk_gate),
        "small_account_minimum_gate_passes": bool(minimum_gate),
        "small_account_executable_gate_passes": bool(executable_gate),
        "full": full,
        "wf": wf,
        "production_blockers": [
            "V35 risk budget is local research evidence only, not live-trading approval",
            "ETF benchmark data is not production vendor entitlement evidence",
            "requires broker paper fills, position reconciliation and external production evidence gate",
        ],
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "name",
        "capital",
        "risk_profile",
        "rank_mode",
        "max_assets",
        "rebalance_freq",
        "target_vol",
        "daily_var_limit",
        "risk_gate_passes",
        "small_account_minimum_gate_passes",
        "small_account_executable_gate_passes",
        "sharpe_ratio",
        "annual_return",
        "annual_volatility",
        "max_drawdown",
        "win_rate",
        "total_return",
        "final_nav",
        "avg_holdings",
        "avg_risky_exposure",
        "avg_cash_weight",
        "avg_target_budget",
        "max_target_budget",
        "zero_budget_rebalances",
        "total_fees",
        "fees_pct_initial_capital",
        "orders",
        "avg_oos_sharpe",
        "min_oos_sharpe",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            full = row["full"]
            wf = row.get("wf") or {}
            oos_values = [fold.get("oos") for fold in wf.get("folds", []) if fold.get("oos") is not None]
            scenario = row["scenario"]
            writer.writerow(
                {
                    "name": row["name"],
                    "capital": scenario["capital"],
                    "risk_profile": scenario["risk_profile"],
                    "rank_mode": scenario["rank_mode"],
                    "max_assets": scenario["max_assets"],
                    "rebalance_freq": scenario["rebalance_freq"],
                    "target_vol": scenario["target_vol"],
                    "daily_var_limit": scenario["daily_var_limit"],
                    "risk_gate_passes": row["risk_gate_passes"],
                    "small_account_minimum_gate_passes": row["small_account_minimum_gate_passes"],
                    "small_account_executable_gate_passes": row["small_account_executable_gate_passes"],
                    "sharpe_ratio": full.get("sharpe_ratio"),
                    "annual_return": full.get("annual_return"),
                    "annual_volatility": full.get("annual_volatility"),
                    "max_drawdown": full.get("max_drawdown"),
                    "win_rate": full.get("win_rate"),
                    "total_return": full.get("total_return"),
                    "final_nav": full.get("final_nav"),
                    "avg_holdings": full.get("avg_holdings"),
                    "avg_risky_exposure": full.get("avg_risky_exposure"),
                    "avg_cash_weight": full.get("avg_cash_weight"),
                    "avg_target_budget": full.get("avg_target_budget"),
                    "max_target_budget": full.get("max_target_budget"),
                    "zero_budget_rebalances": full.get("zero_budget_rebalances"),
                    "total_fees": full.get("total_fees"),
                    "fees_pct_initial_capital": full.get("fees_pct_initial_capital"),
                    "orders": full.get("orders"),
                    "avg_oos_sharpe": wf.get("avg_oos_sharpe"),
                    "min_oos_sharpe": round(float(min(oos_values)), 4) if oos_values else None,
                }
            )


def main() -> None:
    started = time.time()
    args = _parse_args()
    benchmark_dir = Path(args.benchmark_dir)
    output_json = Path(args.output_json)
    output_csv = Path(args.output_csv)
    symbols = _parse_str_list(args.symbols)
    capitals = _parse_float_list(args.capitals)
    max_assets = _parse_int_list(args.max_assets)
    rebalance_freqs = _parse_int_list(args.rebalance_freqs)
    rank_modes = _parse_str_list(args.rank_modes)
    risk_profiles = _parse_str_list(args.risk_profiles)
    target_vols = _parse_float_list(args.target_vols)
    daily_var_limits = _parse_float_list(args.daily_var_limits)
    invalid_modes = sorted(set(rank_modes) - {"momentum", "risk_adjusted", "defensive"})
    if invalid_modes:
        raise ValueError(f"unsupported rank modes: {invalid_modes}")
    invalid_profiles = sorted(set(risk_profiles) - set(RISK_PROFILES))
    if invalid_profiles:
        raise ValueError(f"unsupported risk profiles: {invalid_profiles}")
    cost = CostConfig(
        commission_rate=float(args.commission_rate),
        min_commission=float(args.min_commission),
        stamp_duty_rate=0.0,
        slippage_bps=float(args.slippage_bps),
    )
    close, metadata = _load_etf_panel(benchmark_dir, symbols)
    close = close.loc[pd.Timestamp(args.start_date) : pd.Timestamp(args.end_date)].copy()
    close = close.dropna(how="all")
    if len(close) < 600:
        raise RuntimeError("not enough ETF data for V35 validation")
    scenarios = [
        RiskBudgetScenario(
            capital=capital,
            max_assets=asset_count,
            rebalance_freq=freq,
            rank_mode=rank_mode,  # type: ignore[arg-type]
            risk_profile=risk_profile,  # type: ignore[arg-type]
            target_vol=target_vol,
            daily_var_limit=daily_var_limit,
        )
        for capital in capitals
        for asset_count in max_assets
        for freq in rebalance_freqs
        for rank_mode in rank_modes
        for risk_profile in risk_profiles
        for target_vol in target_vols
        for daily_var_limit in daily_var_limits
    ]
    rows = [_run_scenario(close, scenario, cost) for scenario in scenarios]
    rows.sort(
        key=lambda row: (
            bool(row["small_account_minimum_gate_passes"]),
            bool(row["risk_gate_passes"]),
            bool(row["small_account_executable_gate_passes"]),
            float(row["full"].get("sharpe_ratio", -999)),
            float(row["full"].get("annual_return", -999)),
        ),
        reverse=True,
    )
    best_by_capital: dict[str, Any] = {}
    best_risk_gate_by_capital: dict[str, Any] = {}
    for capital in capitals:
        capital_rows = [row for row in rows if float(row["scenario"]["capital"]) == float(capital)]
        if capital_rows:
            best_by_capital[str(int(capital))] = capital_rows[0]
        risk_gate_rows = [row for row in capital_rows if row["risk_gate_passes"]]
        if risk_gate_rows:
            best_risk_gate_by_capital[str(int(capital))] = risk_gate_rows[0]
    report = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V35-small-account-risk-budget-v1",
        "research_only": True,
        "production_ready": False,
        "objective": "small-account ETF risk budgeting with target volatility, historical VaR and drawdown guards",
        "benchmark_dir": str(benchmark_dir),
        "symbols_requested": symbols,
        "coverage": {
            "assets_loaded": int(close.shape[1]),
            "dates": int(len(close)),
            "start": pd.Timestamp(close.index.min()).date().isoformat(),
            "end": pd.Timestamp(close.index.max()).date().isoformat(),
            "asset_metadata": metadata,
        },
        "risk_profiles": RISK_PROFILES,
        "cost_model": asdict(cost),
        "scenario_count": int(len(rows)),
        "risk_gate_pass_count": int(sum(1 for row in rows if row["risk_gate_passes"])),
        "minimum_gate_pass_count": int(sum(1 for row in rows if row["small_account_minimum_gate_passes"])),
        "best_by_capital": best_by_capital,
        "best_risk_gate_by_capital": best_risk_gate_by_capital,
        "results": rows,
        "production_blockers": [
            "research result does not approve live trading",
            "requires production ETF vendor entitlement, broker order/fill replay and 90-day paper trading",
            "requires external WORM/Secret/Approval/Position/Capacity/DR production evidence gate",
        ],
        "elapsed_seconds": round(time.time() - started, 2),
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(output_csv, rows)
    print(
        json.dumps(
            {
                "production_ready": report["production_ready"],
                "scenario_count": report["scenario_count"],
                "risk_gate_pass_count": report["risk_gate_pass_count"],
                "minimum_gate_pass_count": report["minimum_gate_pass_count"],
                "best_by_capital": {
                    capital: {
                        "name": row["name"],
                        "sharpe": row["full"].get("sharpe_ratio"),
                        "annual_return": row["full"].get("annual_return"),
                        "max_drawdown": row["full"].get("max_drawdown"),
                        "avg_risky_exposure": row["full"].get("avg_risky_exposure"),
                        "final_nav": row["full"].get("final_nav"),
                    }
                    for capital, row in best_by_capital.items()
                },
                "best_risk_gate_by_capital": {
                    capital: {
                        "name": row["name"],
                        "sharpe": row["full"].get("sharpe_ratio"),
                        "annual_return": row["full"].get("annual_return"),
                        "max_drawdown": row["full"].get("max_drawdown"),
                        "avg_risky_exposure": row["full"].get("avg_risky_exposure"),
                        "final_nav": row["full"].get("final_nav"),
                    }
                    for capital, row in best_risk_gate_by_capital.items()
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
