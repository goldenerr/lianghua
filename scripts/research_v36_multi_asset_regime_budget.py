#!/usr/bin/env python3
"""Research V36: small-account multi-asset ETF regime risk budget.

V35 proved that volatility/VaR budgeting can control drawdown, but the available
ETF alpha was too weak. V36 adds a regime layer and audits whether the local
multi-asset ETF pool has enough history for small-account validation. Short
history assets are allowed only as research diagnostics, never production proof.
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
from _paths import HEDGE_ASSETS_DIR, PROJECT_DIR, RESULTS_DIR
from research_v29_portfolio_layer import _walk_forward_from_returns
from research_v33_small_account_execution import CostConfig, _metrics
from research_v34_small_account_etf_rotation import _execute_rebalance, _sha256_file

DEFAULT_BENCHMARK_DIR = PROJECT_DIR / "data" / "benchmarks"
DEFAULT_HEDGE_ASSET_DIR = HEDGE_ASSETS_DIR
DEFAULT_OUTPUT_JSON = RESULTS_DIR / "quant_v36_multi_asset_regime_budget.json"
DEFAULT_OUTPUT_CSV = RESULTS_DIR / "quant_v36_multi_asset_regime_budget.csv"
CASH_PROXY = "511880"

CORE_ASSET_SPECS: dict[str, dict[str, str]] = {
    "510300": {"source": "benchmark", "role": "cn_large_equity", "name": "CSI300 ETF"},
    "510500": {"source": "benchmark", "role": "cn_mid_equity", "name": "CSI500 ETF"},
    "159915": {"source": "benchmark", "role": "cn_growth_equity", "name": "ChiNext ETF"},
    "512100": {"source": "benchmark", "role": "cn_small_equity", "name": "CSI1000 ETF"},
    "518880": {"source": "benchmark", "role": "gold_defensive", "name": "Gold ETF"},
    "511880": {"source": "benchmark", "role": "cash_proxy", "name": "Money Market ETF"},
}
EXPANDED_EXTRA_SPECS: dict[str, dict[str, str]] = {
    "588000": {"source": "benchmark", "role": "cn_tech_equity", "name": "STAR50 ETF"},
    "159920": {"source": "hedge", "role": "hk_equity", "name": "Hang Seng ETF"},
    "159980": {"source": "hedge", "role": "commodity_beta", "name": "Nonferrous ETF"},
    "159985": {"source": "hedge", "role": "commodity_inflation", "name": "Soymeal ETF"},
    "511010": {"source": "hedge", "role": "bond_defensive", "name": "Treasury Bond ETF"},
    "511260": {"source": "hedge", "role": "bond_defensive", "name": "10Y Treasury ETF"},
    "513100": {"source": "hedge", "role": "global_equity", "name": "Nasdaq ETF"},
    "513500": {"source": "hedge", "role": "global_equity", "name": "S&P500 ETF"},
}
EQUITY_ROLES = {
    "cn_large_equity",
    "cn_mid_equity",
    "cn_growth_equity",
    "cn_small_equity",
    "cn_tech_equity",
    "hk_equity",
    "global_equity",
}
DEFENSIVE_ROLES = {"gold_defensive", "bond_defensive", "cash_proxy"}
RISK_PROFILES: dict[str, dict[str, float]] = {
    "aggressive": {
        "risk_on_budget": 0.95,
        "neutral_budget": 0.65,
        "cautious_budget": 0.35,
        "risk_off_budget": 0.15,
        "target_vol": 0.13,
        "daily_var_limit": 0.018,
        "max_budget": 0.95,
        "min_signal": 0.0,
        "min_budget_to_trade": 0.03,
        "dd_reduce_threshold": 0.07,
        "dd_reduce_scale": 0.35,
        "dd_stop_threshold": 0.12,
        "dd_stop_scale": 0.0,
    },
    "balanced": {
        "risk_on_budget": 0.75,
        "neutral_budget": 0.50,
        "cautious_budget": 0.25,
        "risk_off_budget": 0.08,
        "target_vol": 0.10,
        "daily_var_limit": 0.014,
        "max_budget": 0.80,
        "min_signal": 0.0,
        "min_budget_to_trade": 0.03,
        "dd_reduce_threshold": 0.06,
        "dd_reduce_scale": 0.25,
        "dd_stop_threshold": 0.10,
        "dd_stop_scale": 0.0,
    },
    "defensive": {
        "risk_on_budget": 0.55,
        "neutral_budget": 0.35,
        "cautious_budget": 0.18,
        "risk_off_budget": 0.05,
        "target_vol": 0.08,
        "daily_var_limit": 0.010,
        "max_budget": 0.60,
        "min_signal": 0.01,
        "min_budget_to_trade": 0.03,
        "dd_reduce_threshold": 0.05,
        "dd_reduce_scale": 0.15,
        "dd_stop_threshold": 0.08,
        "dd_stop_scale": 0.0,
    },
}


@dataclass(frozen=True)
class RegimeBudgetScenario:
    capital: float
    universe_mode: Literal["core_long", "expanded_recent"]
    max_assets: int
    rebalance_freq: int
    rank_mode: Literal["trend", "risk_adjusted", "defensive_carry"]
    risk_profile: Literal["aggressive", "balanced", "defensive"]
    lookback: int = 126
    lot_size: int = 100


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-dir", default=str(DEFAULT_BENCHMARK_DIR))
    parser.add_argument("--hedge-asset-dir", default=str(DEFAULT_HEDGE_ASSET_DIR))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument("--capitals", default="50000,100000,200000")
    parser.add_argument("--universe-modes", default="core_long,expanded_recent")
    parser.add_argument("--max-assets", default="1,2")
    parser.add_argument("--rebalance-freqs", default="20,40")
    parser.add_argument("--rank-modes", default="trend,risk_adjusted")
    parser.add_argument("--risk-profiles", default="aggressive,balanced,defensive")
    parser.add_argument("--start-date", default="20130101")
    parser.add_argument("--end-date", default="20260529")
    parser.add_argument("--long-history-min-days", type=int, default=1800)
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


def _asset_specs_for_mode(universe_mode: str) -> dict[str, dict[str, str]]:
    if universe_mode == "core_long":
        return dict(CORE_ASSET_SPECS)
    if universe_mode == "expanded_recent":
        specs = dict(CORE_ASSET_SPECS)
        specs.update(EXPANDED_EXTRA_SPECS)
        return specs
    raise ValueError(f"unsupported universe mode: {universe_mode}")


def _asset_path(spec: dict[str, str], symbol: str, benchmark_dir: Path, hedge_asset_dir: Path) -> Path:
    if spec["source"] == "benchmark":
        return benchmark_dir / f"etf_{symbol}.parquet"
    if spec["source"] == "hedge":
        return hedge_asset_dir / f"etf_{symbol}.parquet"
    raise ValueError(f"unsupported source for {symbol}: {spec['source']}")


def _load_multi_asset_panel(
    benchmark_dir: Path,
    hedge_asset_dir: Path,
    universe_mode: str,
    *,
    long_history_min_days: int,
) -> tuple[pd.DataFrame, dict[str, Any], dict[str, str]]:
    specs = _asset_specs_for_mode(universe_mode)
    close: dict[str, pd.Series] = {}
    metadata: dict[str, Any] = {}
    roles: dict[str, str] = {}
    for symbol, spec in specs.items():
        path = _asset_path(spec, symbol, benchmark_dir, hedge_asset_dir)
        roles[symbol] = spec["role"]
        if not path.exists():
            metadata[symbol] = {
                "exists": False,
                "path": str(path),
                "role": spec["role"],
                "source_group": spec["source"],
                "name": spec["name"],
                "long_history_ready": False,
            }
            continue
        frame = pd.read_parquet(path)
        if "close" not in frame.columns:
            metadata[symbol] = {
                "exists": True,
                "path": str(path),
                "role": spec["role"],
                "source_group": spec["source"],
                "name": spec["name"],
                "failure": "missing close",
                "long_history_ready": False,
            }
            continue
        series = pd.to_numeric(frame["close"], errors="coerce")
        series.index = pd.to_datetime(frame.index, errors="coerce").normalize()
        series = series.loc[series.index.notna()].sort_index().dropna()
        if series.empty:
            metadata[symbol] = {
                "exists": True,
                "path": str(path),
                "role": spec["role"],
                "source_group": spec["source"],
                "name": spec["name"],
                "failure": "empty close",
                "long_history_ready": False,
            }
            continue
        close[symbol] = series
        metadata[symbol] = {
            "exists": True,
            "path": str(path),
            "role": spec["role"],
            "source_group": spec["source"],
            "name": spec["name"],
            "sha256": _sha256_file(path),
            "rows": int(len(series)),
            "start": series.index.min().date().isoformat(),
            "end": series.index.max().date().isoformat(),
            "first_close": round(float(series.iloc[0]), 4),
            "last_close": round(float(series.iloc[-1]), 4),
            "one_lot_value_latest": round(float(series.iloc[-1]) * 100, 2),
            "long_history_ready": bool(len(series) >= long_history_min_days),
        }
    if not close:
        raise RuntimeError(f"no ETF close data available for universe_mode={universe_mode}")
    panel = pd.DataFrame(close).sort_index()
    return panel, metadata, roles


def _coverage_summary(metadata: dict[str, Any], *, long_history_min_days: int) -> dict[str, Any]:
    loaded = {symbol: item for symbol, item in metadata.items() if item.get("exists") and not item.get("failure")}
    long_ready = {symbol: item for symbol, item in loaded.items() if item.get("long_history_ready")}
    short_history = {symbol: item for symbol, item in loaded.items() if not item.get("long_history_ready")}
    missing = {symbol: item for symbol, item in metadata.items() if not item.get("exists") or item.get("failure")}
    return {
        "long_history_min_days": int(long_history_min_days),
        "loaded_assets": int(len(loaded)),
        "long_history_assets": int(len(long_ready)),
        "short_history_assets": int(len(short_history)),
        "missing_or_invalid_assets": int(len(missing)),
        "long_history_symbols": sorted(long_ready),
        "short_history_symbols": sorted(short_history),
        "missing_or_invalid_symbols": sorted(missing),
        "short_history_only": bool(short_history),
    }


def _score_assets(
    close: pd.DataFrame,
    signal_idx: int,
    scenario: RegimeBudgetScenario,
    roles: dict[str, str],
) -> pd.Series:
    if signal_idx < scenario.lookback:
        return pd.Series(dtype=float)
    window = close.iloc[: signal_idx + 1]
    current = window.iloc[-1]
    valid_current = current.dropna().index
    mom_20 = current / window.shift(20).iloc[-1] - 1.0
    mom_60 = current / window.shift(60).iloc[-1] - 1.0
    mom_120 = current / window.shift(120).iloc[-1] - 1.0
    returns_60 = window.pct_change(fill_method=None).tail(60)
    vol_60 = returns_60.std(ddof=1) * np.sqrt(252)
    drawdown_60 = current / window.tail(60).max() - 1.0
    if scenario.rank_mode == "trend":
        score = 0.30 * mom_20 + 0.40 * mom_60 + 0.30 * mom_120 - 0.15 * vol_60 + 0.10 * drawdown_60
    elif scenario.rank_mode == "risk_adjusted":
        score = 0.25 * mom_20 + 0.45 * mom_60 + 0.30 * mom_120 - 0.45 * vol_60 + 0.15 * drawdown_60
    elif scenario.rank_mode == "defensive_carry":
        score = 0.20 * mom_20 + 0.35 * mom_60 + 0.25 * mom_120 - 0.30 * vol_60 + 0.20 * drawdown_60
        for symbol, role in roles.items():
            if role in {"gold_defensive", "bond_defensive"} and symbol in score.index:
                score.loc[symbol] += 0.02
    else:
        raise ValueError(f"unsupported rank_mode: {scenario.rank_mode}")
    score = score.loc[score.index.intersection(valid_current)]
    score = score.drop(labels=[CASH_PROXY], errors="ignore")
    return score.replace([np.inf, -np.inf], np.nan).dropna()


def _market_regime(
    close: pd.DataFrame,
    signal_idx: int,
    roles: dict[str, str],
) -> tuple[str, dict[str, float]]:
    equity_symbols = [
        symbol
        for symbol, role in roles.items()
        if role in EQUITY_ROLES and symbol in close.columns and pd.notna(close.iloc[signal_idx].get(symbol))
    ]
    diagnostics = {
        "equity_breadth_60d": 0.0,
        "equity_composite_momentum": 0.0,
        "equity_realized_vol_60d": 0.0,
    }
    if signal_idx < 120 or not equity_symbols:
        return "risk_off", diagnostics
    window = close[equity_symbols].iloc[: signal_idx + 1].ffill()
    current = window.iloc[-1]
    mom_60 = current / window.shift(60).iloc[-1] - 1.0
    mom_120 = current / window.shift(120).iloc[-1] - 1.0
    combined = (0.60 * mom_60 + 0.40 * mom_120).replace([np.inf, -np.inf], np.nan).dropna()
    if combined.empty:
        return "risk_off", diagnostics
    equal_weight_returns = window[combined.index].mean(axis=1).pct_change(fill_method=None).tail(60).dropna()
    realized_vol = float(equal_weight_returns.std(ddof=1) * np.sqrt(252)) if len(equal_weight_returns) > 10 else 0.0
    breadth = float((mom_60.loc[combined.index] > 0.0).mean())
    composite = float(combined.mean())
    diagnostics.update(
        {
            "equity_breadth_60d": breadth,
            "equity_composite_momentum": composite,
            "equity_realized_vol_60d": realized_vol,
        }
    )
    if composite > 0.08 and breadth >= 0.60 and realized_vol < 0.28:
        return "risk_on", diagnostics
    if composite > 0.00 and breadth >= 0.40:
        return "neutral", diagnostics
    if composite < -0.06 or breadth <= 0.20:
        return "risk_off", diagnostics
    return "cautious", diagnostics


def _risk_symbols(score: pd.Series, scenario: RegimeBudgetScenario) -> list[str]:
    profile = RISK_PROFILES[scenario.risk_profile]
    candidates = score.drop(labels=[CASH_PROXY], errors="ignore")
    candidates = candidates[candidates >= float(profile["min_signal"])]
    if candidates.empty:
        return []
    return [str(symbol) for symbol in candidates.sort_values(ascending=False).head(scenario.max_assets).index]


def _historical_budget(
    close: pd.DataFrame,
    *,
    signal_idx: int,
    selected: list[str],
    scenario: RegimeBudgetScenario,
    nav: float,
    peak_nav: float,
    roles: dict[str, str],
) -> tuple[float, dict[str, float | str]]:
    profile = RISK_PROFILES[scenario.risk_profile]
    regime, regime_diag = _market_regime(close, signal_idx, roles)
    base_budget = float(profile[f"{regime}_budget"])
    diagnostics: dict[str, float | str] = {
        "regime": regime,
        "base_budget": base_budget,
        "budget_before_guards": 0.0,
        "realized_vol": 0.0,
        "daily_var95": 0.0,
        "vol_scale": 0.0,
        "var_scale": 0.0,
        "drawdown_scale": 1.0,
        "account_drawdown": 0.0,
        **regime_diag,
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
    vol_scale = min(1.0, float(profile["target_vol"]) / realized_vol)
    var_scale = 1.0 if daily_var95 <= 1e-12 else min(1.0, float(profile["daily_var_limit"]) / daily_var95)
    budget = min(float(profile["max_budget"]), base_budget) * min(vol_scale, var_scale)
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
    return max(0.0, min(float(profile["max_budget"]), float(budget))), diagnostics


def _target_weights(
    close: pd.DataFrame,
    *,
    signal_idx: int,
    score: pd.Series,
    scenario: RegimeBudgetScenario,
    nav: float,
    peak_nav: float,
    roles: dict[str, str],
) -> tuple[dict[str, float], dict[str, float | str]]:
    selected = _risk_symbols(score, scenario)
    budget, diagnostics = _historical_budget(
        close,
        signal_idx=signal_idx,
        selected=selected,
        scenario=scenario,
        nav=nav,
        peak_nav=peak_nav,
        roles=roles,
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


def _run_scenario(
    close: pd.DataFrame,
    scenario: RegimeBudgetScenario,
    cost: CostConfig,
    roles: dict[str, str],
    coverage: dict[str, Any],
) -> dict[str, Any]:
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
    budget_diagnostics: list[dict[str, float | str]] = []
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
            score = _score_assets(close, i - 1, scenario, roles)
            target_weights, budget_diag = _target_weights(
                close,
                signal_idx=i - 1,
                score=score,
                scenario=scenario,
                nav=nav_before,
                peak_nav=peak_nav,
                roles=roles,
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
    regime_counts: dict[str, int] = {}
    for item in budget_diagnostics:
        regime = str(item.get("regime", "unknown"))
        regime_counts[regime] = regime_counts.get(regime, 0) + 1
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
                float(np.mean([float(item["realized_vol"]) for item in budget_diagnostics])),
                4,
            )
            if budget_diagnostics
            else 0.0,
            "avg_daily_var95_at_rebalance": round(
                float(np.mean([float(item["daily_var95"]) for item in budget_diagnostics])),
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
            "regime_counts": regime_counts,
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
    long_history_research_gate = (
        scenario.universe_mode == "core_long"
        and not coverage["short_history_only"]
        and bool(minimum_gate)
        and bool(executable_gate)
    )
    return {
        "name": (
            f"v36_regime_{scenario.universe_mode}_{scenario.risk_profile}_{scenario.rank_mode}_"
            f"{int(scenario.capital)}_{scenario.max_assets}asset_{scenario.rebalance_freq}d"
        ),
        "scenario": asdict(scenario),
        "risk_profile_params": RISK_PROFILES[scenario.risk_profile],
        "cost": asdict(cost),
        "research_only": True,
        "production_ready": False,
        "risk_gate_passes": bool(risk_gate),
        "small_account_minimum_gate_passes": bool(minimum_gate),
        "small_account_executable_gate_passes": bool(executable_gate),
        "long_history_research_gate_passes": bool(long_history_research_gate),
        "full": full,
        "wf": wf,
        "production_blockers": [
            "V36 is local research evidence only, not live-trading approval",
            "expanded_recent universe contains short-history assets and cannot prove long-cycle robustness",
            "requires production ETF vendor entitlement, broker order/fill replay and 90-day paper trading",
            "requires external WORM/Secret/Approval/Position/Capacity/DR production evidence gate",
        ],
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "name",
        "capital",
        "universe_mode",
        "risk_profile",
        "rank_mode",
        "max_assets",
        "rebalance_freq",
        "risk_gate_passes",
        "small_account_minimum_gate_passes",
        "small_account_executable_gate_passes",
        "long_history_research_gate_passes",
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
                    "universe_mode": scenario["universe_mode"],
                    "risk_profile": scenario["risk_profile"],
                    "rank_mode": scenario["rank_mode"],
                    "max_assets": scenario["max_assets"],
                    "rebalance_freq": scenario["rebalance_freq"],
                    "risk_gate_passes": row["risk_gate_passes"],
                    "small_account_minimum_gate_passes": row["small_account_minimum_gate_passes"],
                    "small_account_executable_gate_passes": row["small_account_executable_gate_passes"],
                    "long_history_research_gate_passes": row["long_history_research_gate_passes"],
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
    hedge_asset_dir = Path(args.hedge_asset_dir)
    output_json = Path(args.output_json)
    output_csv = Path(args.output_csv)
    capitals = _parse_float_list(args.capitals)
    universe_modes = _parse_str_list(args.universe_modes)
    max_assets = _parse_int_list(args.max_assets)
    rebalance_freqs = _parse_int_list(args.rebalance_freqs)
    rank_modes = _parse_str_list(args.rank_modes)
    risk_profiles = _parse_str_list(args.risk_profiles)
    invalid_universes = sorted(set(universe_modes) - {"core_long", "expanded_recent"})
    if invalid_universes:
        raise ValueError(f"unsupported universe modes: {invalid_universes}")
    invalid_modes = sorted(set(rank_modes) - {"trend", "risk_adjusted", "defensive_carry"})
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
    panels: dict[str, pd.DataFrame] = {}
    metadata_by_universe: dict[str, Any] = {}
    coverage_by_universe: dict[str, Any] = {}
    roles_by_universe: dict[str, dict[str, str]] = {}
    for universe_mode in universe_modes:
        panel, metadata, roles = _load_multi_asset_panel(
            benchmark_dir,
            hedge_asset_dir,
            universe_mode,
            long_history_min_days=int(args.long_history_min_days),
        )
        panel = panel.loc[pd.Timestamp(args.start_date) : pd.Timestamp(args.end_date)].copy()
        panel = panel.dropna(how="all")
        if len(panel) < 600:
            raise RuntimeError(f"not enough ETF data for V36 validation: {universe_mode}")
        panels[universe_mode] = panel
        metadata_by_universe[universe_mode] = metadata
        coverage_by_universe[universe_mode] = _coverage_summary(
            metadata,
            long_history_min_days=int(args.long_history_min_days),
        )
        roles_by_universe[universe_mode] = roles
    scenarios = [
        RegimeBudgetScenario(
            capital=capital,
            universe_mode=universe_mode,  # type: ignore[arg-type]
            max_assets=asset_count,
            rebalance_freq=freq,
            rank_mode=rank_mode,  # type: ignore[arg-type]
            risk_profile=risk_profile,  # type: ignore[arg-type]
        )
        for capital in capitals
        for universe_mode in universe_modes
        for asset_count in max_assets
        for freq in rebalance_freqs
        for rank_mode in rank_modes
        for risk_profile in risk_profiles
    ]
    rows = [
        _run_scenario(
            panels[scenario.universe_mode],
            scenario,
            cost,
            roles_by_universe[scenario.universe_mode],
            coverage_by_universe[scenario.universe_mode],
        )
        for scenario in scenarios
    ]
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
    best_by_universe_capital: dict[str, Any] = {}
    for capital in capitals:
        capital_rows = [row for row in rows if float(row["scenario"]["capital"]) == float(capital)]
        if capital_rows:
            best_by_capital[str(int(capital))] = capital_rows[0]
        for universe_mode in universe_modes:
            subset = [
                row
                for row in capital_rows
                if str(row["scenario"]["universe_mode"]) == universe_mode
            ]
            if subset:
                best_by_universe_capital[f"{universe_mode}:{int(capital)}"] = subset[0]
    report = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V36-multi-asset-regime-budget-v1",
        "research_only": True,
        "production_ready": False,
        "objective": "small-account multi-asset ETF regime risk budgeting with coverage audit",
        "benchmark_dir": str(benchmark_dir),
        "hedge_asset_dir": str(hedge_asset_dir),
        "coverage_by_universe": coverage_by_universe,
        "asset_metadata_by_universe": metadata_by_universe,
        "risk_profiles": RISK_PROFILES,
        "cost_model": asdict(cost),
        "scenario_count": int(len(rows)),
        "risk_gate_pass_count": int(sum(1 for row in rows if row["risk_gate_passes"])),
        "minimum_gate_pass_count": int(sum(1 for row in rows if row["small_account_minimum_gate_passes"])),
        "long_history_research_gate_pass_count": int(
            sum(1 for row in rows if row["long_history_research_gate_passes"])
        ),
        "best_by_capital": best_by_capital,
        "best_by_universe_capital": best_by_universe_capital,
        "results": rows,
        "production_blockers": [
            "research result does not approve live trading",
            "expanded recent assets start in 2024 and cannot serve as long-cycle production evidence",
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
                "long_history_research_gate_pass_count": report["long_history_research_gate_pass_count"],
                "coverage_by_universe": report["coverage_by_universe"],
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
                "output_json": str(output_json),
                "output_csv": str(output_csv),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
