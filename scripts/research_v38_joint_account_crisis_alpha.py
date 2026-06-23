#!/usr/bin/env python3
"""Research V38: joint-account execution and long-history crisis alpha.

The engine executes V36 ETFs, V32/V29-derived stock alpha and a gold trend
crisis sleeve in one cash account. Orders use 100-share lots, side-specific
fees and V31 stock trading constraints. All signals use prior-day or older
information. This remains research-only and never approves live trading.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import time
from collections.abc import Callable
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
    _affordable_buy_shares,
    _metrics,
    _order_fee,
    _round_down_to_lot,
    _score_for_mode,
    _select_small_account_targets,
    _status_value,
)
from research_v33_small_account_execution import (
    _run_scenario as _run_stock_scenario,
)
from research_v36_multi_asset_regime_budget import (
    RegimeBudgetScenario,
    _coverage_summary,
    _load_multi_asset_panel,
    _market_regime,
)
from research_v36_multi_asset_regime_budget import (
    _run_scenario as _run_v36_scenario,
)
from research_v36_multi_asset_regime_budget import (
    _score_assets as _score_etfs,
)
from research_v36_multi_asset_regime_budget import (
    _target_weights as _target_etf_weights,
)
from research_v37_stock_alpha_multi_asset_budget import REGIME_ALPHA_MULTIPLIERS

DEFAULT_BENCHMARK_DIR = PROJECT_DIR / "data" / "benchmarks"
DEFAULT_OUTPUT_JSON = RESULTS_DIR / "quant_v38_joint_account_crisis_alpha.json"
DEFAULT_OUTPUT_CSV = RESULTS_DIR / "quant_v38_joint_account_crisis_alpha.csv"
GOLD_SYMBOL = "518880"
CASH_PROXY = "511880"
TARGET_RISK_CAPACITY = 0.94
RISK_GATE_LIMIT = 0.95


@dataclass(frozen=True)
class JointScenario:
    capital: float
    stock_alpha_cap: float
    crisis_cap: float
    stock_positions: int = 2
    stock_rebalance_freq: int = 40
    etf_rebalance_freq: int = 20
    stock_profile: Literal["small_balanced"] = "small_balanced"
    stock_rank_mode: Literal["blend"] = "blend"
    stock_lookback: int = 60
    lot_size: int = 100


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-dir", default=str(DEFAULT_BENCHMARK_DIR))
    parser.add_argument("--hedge-asset-dir", default=str(HEDGE_ASSETS_DIR))
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    parser.add_argument("--trading-status-path", default=str(DEFAULT_V31_TRADING_STATUS))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument("--capitals", default="50000,100000,200000")
    parser.add_argument("--stock-alpha-caps", default="0.05,0.10,0.15")
    parser.add_argument("--crisis-caps", default="0.00,0.10,0.15")
    parser.add_argument("--start-date", default="20130101")
    parser.add_argument("--end-date", default="20260529")
    parser.add_argument("--long-history-min-days", type=int, default=1800)
    parser.add_argument("--commission-rate", type=float, default=0.00025)
    parser.add_argument("--min-commission", type=float, default=5.0)
    parser.add_argument("--stock-stamp-duty-rate", type=float, default=0.0005)
    parser.add_argument("--stock-slippage-bps", type=float, default=5.0)
    parser.add_argument("--etf-slippage-bps", type=float, default=3.0)
    return parser.parse_args()


def _parse_float_list(raw: str) -> list[float]:
    values = [float(item.strip()) for item in raw.split(",") if item.strip()]
    if not values:
        raise ValueError("expected at least one numeric value")
    return values


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _group_for_symbol(
    symbol: str,
    stock_symbols: set[str],
    crisis_symbols: set[str] | None = None,
) -> str:
    if symbol in stock_symbols:
        return "stock_alpha"
    if symbol in (crisis_symbols or {GOLD_SYMBOL}):
        return "crisis_gold"
    return "etf_core"


def _crisis_target_weight(
    close: pd.DataFrame,
    *,
    signal_idx: int,
    regime: str,
    crisis_cap: float,
) -> tuple[float, dict[str, Any]]:
    diagnostics = {
        "regime": regime,
        "gold_momentum_60d": 0.0,
        "gold_momentum_120d": 0.0,
        "gold_above_ma120": False,
        "target_weight": 0.0,
    }
    if crisis_cap <= 0.0 or GOLD_SYMBOL not in close.columns or signal_idx < 120:
        return 0.0, diagnostics
    gold = pd.to_numeric(close[GOLD_SYMBOL].iloc[: signal_idx + 1], errors="coerce").dropna()
    if len(gold) < 121:
        return 0.0, diagnostics
    current = float(gold.iloc[-1])
    mom_60 = current / float(gold.iloc[-61]) - 1.0
    mom_120 = current / float(gold.iloc[-121]) - 1.0
    above_ma = current > float(gold.tail(120).mean())
    diagnostics.update(
        {
            "gold_momentum_60d": round(mom_60, 6),
            "gold_momentum_120d": round(mom_120, 6),
            "gold_above_ma120": bool(above_ma),
        }
    )
    if mom_60 <= 0.0 or mom_120 <= 0.0 or not above_ma:
        return 0.0, diagnostics
    regime_scale = {"risk_off": 1.0, "cautious": 0.60, "neutral": 0.25, "risk_on": 0.0}.get(
        regime, 0.0
    )
    target = max(0.0, min(crisis_cap, crisis_cap * regime_scale))
    diagnostics["target_weight"] = round(target, 6)
    return target, diagnostics


def _stock_target_budget(
    gate_returns: pd.Series,
    *,
    signal_date: pd.Timestamp,
    regime: str,
    stock_alpha_cap: float,
    nav: float,
    peak_nav: float,
    lookback: int,
) -> tuple[float, dict[str, Any]]:
    diagnostics = {
        "regime": regime,
        "trailing_stock_return": 0.0,
        "regime_scale": 0.0,
        "drawdown_scale": 1.0,
        "target_budget": 0.0,
    }
    if stock_alpha_cap <= 0.0:
        return 0.0, diagnostics
    history = pd.to_numeric(
        gate_returns.loc[gate_returns.index <= signal_date].tail(lookback), errors="coerce"
    ).dropna()
    if len(history) < lookback:
        return 0.0, diagnostics
    trailing = float((1.0 + history).prod() - 1.0)
    regime_scale = float(REGIME_ALPHA_MULTIPLIERS.get(regime, 0.0))
    if trailing <= 0.0:
        regime_scale = 0.0
    account_drawdown = nav / peak_nav - 1.0 if peak_nav > 0 else 0.0
    drawdown_scale = 1.0
    if account_drawdown <= -0.08:
        drawdown_scale = 0.0
    elif account_drawdown <= -0.05:
        drawdown_scale = 0.25
    target = stock_alpha_cap * regime_scale * drawdown_scale
    diagnostics.update(
        {
            "trailing_stock_return": round(trailing, 6),
            "regime_scale": round(regime_scale, 4),
            "drawdown_scale": round(drawdown_scale, 4),
            "target_budget": round(target, 6),
        }
    )
    return max(0.0, min(stock_alpha_cap, target)), diagnostics


def _merge_target_weights(
    *,
    etf_weights: dict[str, float],
    stock_symbols: list[str],
    stock_budget: float,
    crisis_weight: float = 0.0,
    crisis_weights: dict[str, float] | None = None,
) -> tuple[dict[str, float], dict[str, float]]:
    clean_etf = {
        str(symbol): max(0.0, float(weight))
        for symbol, weight in etf_weights.items()
        if symbol != CASH_PROXY and float(weight) > 1e-12
    }
    etf_total = float(sum(clean_etf.values()))
    if etf_total > TARGET_RISK_CAPACITY:
        scale = TARGET_RISK_CAPACITY / etf_total
        clean_etf = {symbol: weight * scale for symbol, weight in clean_etf.items()}
        etf_total = TARGET_RISK_CAPACITY
    headroom = max(0.0, TARGET_RISK_CAPACITY - etf_total)
    requested_crisis = (
        {GOLD_SYMBOL: max(0.0, crisis_weight)}
        if crisis_weights is None
        else {
            str(symbol): max(0.0, float(weight))
            for symbol, weight in crisis_weights.items()
            if float(weight) > 1e-12
        }
    )
    requested_crisis_total = float(sum(requested_crisis.values()))
    crisis_alloc = min(requested_crisis_total, headroom)
    crisis_scale = (
        crisis_alloc / requested_crisis_total if requested_crisis_total > 1e-12 else 0.0
    )
    headroom -= crisis_alloc
    stock_alloc = min(max(0.0, stock_budget), headroom)
    merged = dict(clean_etf)
    if crisis_scale > 0.0:
        for symbol, weight in requested_crisis.items():
            merged[symbol] = merged.get(symbol, 0.0) + weight * crisis_scale
    if stock_alloc > 1e-12 and stock_symbols:
        per_stock = stock_alloc / len(stock_symbols)
        for symbol in stock_symbols:
            merged[str(symbol)] = merged.get(str(symbol), 0.0) + per_stock
    total = float(sum(merged.values()))
    if total > TARGET_RISK_CAPACITY + 1e-10:
        raise RuntimeError(f"target risk capacity exceeded: {total}")
    return merged, {
        "etf_target": round(etf_total, 6),
        "crisis_target": round(crisis_alloc, 6),
        "stock_target": round(stock_alloc, 6),
        "total_target": round(total, 6),
        "cash_target": round(max(0.0, 1.0 - total), 6),
    }


def _execute_joint_rebalance(
    *,
    positions: dict[str, int],
    cash: float,
    target_weights: dict[str, float],
    nav: float,
    prices: pd.Series,
    trade_date: pd.Timestamp,
    stock_symbols: set[str],
    trading_constraints: dict[str, pd.DataFrame],
    stock_cost: CostConfig,
    etf_cost: CostConfig,
    lot_size: int,
    crisis_symbols: set[str] | None = None,
) -> tuple[dict[str, int], float, dict[str, Any]]:
    diagnostics: dict[str, Any] = {
        "orders": 0,
        "buy_orders": 0,
        "sell_orders": 0,
        "buy_notional": 0.0,
        "sell_notional": 0.0,
        "fees": 0.0,
        "fees_by_group": {"etf_core": 0.0, "crisis_gold": 0.0, "stock_alpha": 0.0},
        "blocked_buys": 0,
        "blocked_sells": 0,
        "lot_blocked_targets": 0,
        "cash_blocked_buys": 0,
        "missing_price_blocks": 0,
    }
    target_shares: dict[str, int] = {}
    for symbol, weight in target_weights.items():
        price = float(prices.get(symbol, np.nan))
        if not math.isfinite(price) or price <= 0:
            diagnostics["missing_price_blocks"] += 1
            continue
        shares = _round_down_to_lot(nav * float(weight) / price, lot_size)
        if shares <= 0:
            diagnostics["lot_blocked_targets"] += 1
            continue
        target_shares[symbol] = shares

    updated = {symbol: int(shares) for symbol, shares in positions.items() if shares > 0}
    for symbol, current in list(updated.items()):
        desired = int(target_shares.get(symbol, 0))
        sell_shares = current - desired
        if sell_shares <= 0:
            continue
        price = float(prices.get(symbol, np.nan))
        if not math.isfinite(price) or price <= 0:
            diagnostics["missing_price_blocks"] += 1
            continue
        if symbol in stock_symbols:
            tradable = _status_value(
                trading_constraints, "is_tradable", trade_date, symbol, default=False
            )
            limit_down = _status_value(
                trading_constraints, "is_limit_down", trade_date, symbol, default=True
            )
            if not tradable or limit_down:
                diagnostics["blocked_sells"] += 1
                continue
        group = _group_for_symbol(symbol, stock_symbols, crisis_symbols)
        cost = stock_cost if symbol in stock_symbols else etf_cost
        notional = float(sell_shares) * price
        fee = _order_fee(notional, "sell", cost)
        cash += notional - fee
        diagnostics["orders"] += 1
        diagnostics["sell_orders"] += 1
        diagnostics["sell_notional"] += notional
        diagnostics["fees"] += fee
        diagnostics["fees_by_group"][group] += fee
        remaining = current - sell_shares
        if remaining > 0:
            updated[symbol] = remaining
        else:
            updated.pop(symbol, None)

    def buy_priority(symbol: str) -> tuple[int, str]:
        group = _group_for_symbol(symbol, stock_symbols, crisis_symbols)
        priority = {"crisis_gold": 0, "etf_core": 1, "stock_alpha": 2}[group]
        return priority, symbol

    for symbol in sorted(target_shares, key=buy_priority):
        desired = target_shares[symbol]
        current = int(updated.get(symbol, 0))
        buy_shares = desired - current
        if buy_shares <= 0:
            continue
        price = float(prices.get(symbol, np.nan))
        if not math.isfinite(price) or price <= 0:
            diagnostics["missing_price_blocks"] += 1
            continue
        if symbol in stock_symbols:
            tradable = _status_value(
                trading_constraints, "is_tradable", trade_date, symbol, default=False
            )
            limit_up = _status_value(
                trading_constraints, "is_limit_up", trade_date, symbol, default=True
            )
            if not tradable or limit_up:
                diagnostics["blocked_buys"] += 1
                continue
        cost = stock_cost if symbol in stock_symbols else etf_cost
        affordable = _affordable_buy_shares(
            cash=cash,
            price=price,
            desired_shares=buy_shares,
            lot_size=lot_size,
            cost=cost,
        )
        if affordable <= 0:
            diagnostics["cash_blocked_buys"] += 1
            continue
        group = _group_for_symbol(symbol, stock_symbols, crisis_symbols)
        notional = float(affordable) * price
        fee = _order_fee(notional, "buy", cost)
        cash -= notional + fee
        updated[symbol] = current + affordable
        diagnostics["orders"] += 1
        diagnostics["buy_orders"] += 1
        diagnostics["buy_notional"] += notional
        diagnostics["fees"] += fee
        diagnostics["fees_by_group"][group] += fee

    if cash < -1e-6:
        raise RuntimeError(f"shared cash became negative: {cash}")
    return (
        {symbol: shares for symbol, shares in updated.items() if shares > 0},
        max(0.0, cash),
        diagnostics,
    )


def _min_oos(wf: dict[str, Any]) -> float | None:
    values = [float(fold["oos"]) for fold in wf.get("folds", []) if fold.get("oos") is not None]
    return round(min(values), 4) if values else None


def _compare_baseline_returns(
    joint_returns: pd.Series, reference_returns: pd.Series
) -> dict[str, Any]:
    same_index = joint_returns.index.equals(reference_returns.index)
    aligned = pd.concat(
        [joint_returns.rename("joint"), reference_returns.rename("reference")],
        axis=1,
        join="inner",
    ).dropna()
    max_abs_difference = (
        float((aligned["joint"] - aligned["reference"]).abs().max())
        if not aligned.empty
        else math.inf
    )
    passes = bool(
        same_index
        and len(aligned) == len(joint_returns) == len(reference_returns)
        and max_abs_difference <= 1e-12
    )
    return {
        "passes": passes,
        "same_index": bool(same_index),
        "joint_days": int(len(joint_returns)),
        "reference_days": int(len(reference_returns)),
        "max_abs_daily_return_difference": round(max_abs_difference, 14),
    }


def _stock_ready_index(
    *, start_idx: int, raw_ready_idx: int, etf_rebalance_freq: int, align_to_etf: bool
) -> int:
    if not align_to_etf or raw_ready_idx <= start_idx:
        return max(start_idx, raw_ready_idx)
    periods = math.ceil((raw_ready_idx - start_idx) / etf_rebalance_freq)
    return start_idx + periods * etf_rebalance_freq


def _weakest_fold_attribution(
    returns: pd.Series,
    attribution: pd.DataFrame,
    wf: dict[str, Any],
) -> dict[str, Any]:
    folds = [fold for fold in wf.get("folds", []) if fold.get("oos") is not None]
    if not folds:
        return {}
    weakest = min(folds, key=lambda fold: float(fold["oos"]))
    start = pd.Timestamp(weakest["test_start"])
    end = pd.Timestamp(weakest["test_end"])
    mask = (returns.index >= start) & (returns.index <= end)
    sliced_returns = returns.loc[mask]
    sliced_attr = attribution.reindex(sliced_returns.index).fillna(0.0)
    return {
        "fold": int(weakest["fold"]),
        "start": start.date().isoformat(),
        "end": end.date().isoformat(),
        "oos_sharpe": float(weakest["oos"]),
        "max_drawdown": float(weakest["mdd"]),
        "period_total_return": round(float((1.0 + sliced_returns).prod() - 1.0), 6),
        "simple_return_contribution": {
            column: round(float(sliced_attr[column].sum()), 6) for column in sliced_attr.columns
        },
    }


def _run_joint_scenario(
    *,
    scenario: JointScenario,
    etf_close: pd.DataFrame,
    etf_roles: dict[str, str],
    stock_close: pd.DataFrame,
    valuation_stock_close: pd.DataFrame,
    factors: dict[str, pd.DataFrame],
    rolling_ic: pd.DataFrame,
    industry_map: dict[str, str],
    trading_constraints: dict[str, pd.DataFrame],
    stock_gate_returns: pd.Series,
    stock_cost: CostConfig,
    etf_cost: CostConfig,
    crisis_close: pd.DataFrame | None = None,
    crisis_symbols: set[str] | None = None,
    crisis_allocator: Callable[..., tuple[dict[str, float], dict[str, Any]]] | None = None,
    align_stock_rebalance_to_etf: bool = False,
    include_series: bool = False,
) -> dict[str, Any]:
    common_index = pd.DatetimeIndex(etf_close.index).intersection(stock_close.index).sort_values()
    etf_close = etf_close.reindex(common_index)
    stock_close = stock_close.reindex(common_index)
    crisis_close = (crisis_close if crisis_close is not None else etf_close).reindex(common_index)
    active_crisis_symbols = crisis_symbols or {GOLD_SYMBOL}
    execution_etf = etf_close.copy()
    for symbol in active_crisis_symbols:
        if symbol in crisis_close.columns:
            execution_etf[symbol] = crisis_close[symbol]
    valuation_etf = execution_etf.ffill()
    valuation_stock = valuation_stock_close.reindex(common_index).ffill()
    valuation_prices = pd.concat([valuation_etf, valuation_stock], axis=1)
    positions: dict[str, int] = {}
    cash = float(scenario.capital)
    prev_nav = float(scenario.capital)
    peak_nav = float(scenario.capital)
    returns: list[float] = []
    dates: list[pd.Timestamp] = []
    attr_rows: list[dict[str, float]] = []
    exposure_values: list[float] = []
    cash_weights: list[float] = []
    stock_holdings: list[int] = []
    target_rows: list[dict[str, float]] = []
    invariant_max_error = 0.0
    risk_capacity_violations = 0
    current_etf_weights: dict[str, float] = {}
    current_stock_symbols: list[str] = []
    current_stock_budget = 0.0
    current_crisis_weights: dict[str, float] = {}
    diagnostics: dict[str, Any] = {
        "orders": 0,
        "buy_orders": 0,
        "sell_orders": 0,
        "buy_notional": 0.0,
        "sell_notional": 0.0,
        "fees": 0.0,
        "fees_by_group": {"etf_core": 0.0, "crisis_gold": 0.0, "stock_alpha": 0.0},
        "blocked_buys": 0,
        "blocked_sells": 0,
        "lot_blocked_targets": 0,
        "cash_blocked_buys": 0,
        "missing_price_blocks": 0,
    }
    stock_symbol_set = {str(symbol) for symbol in stock_close.columns}
    stock_select_scenario = ScenarioConfig(
        capital=scenario.capital,
        max_positions=scenario.stock_positions,
        rank_mode=scenario.stock_rank_mode,
        gross_profile=scenario.stock_profile,
        rebalance_freq=scenario.stock_rebalance_freq,
        max_per_industry=1,
        candidate_pool_multiplier=10,
        per_position_budget_buffer=1.50,
    )
    etf_scenario = RegimeBudgetScenario(
        capital=scenario.capital,
        universe_mode="core_long",
        max_assets=1,
        rebalance_freq=scenario.etf_rebalance_freq,
        rank_mode="risk_adjusted",
        risk_profile="aggressive",
    )
    # Keep the ETF core on the exact V36 history window. The stock sleeve needs
    # a longer factor warm-up and remains disabled until that history exists.
    start_idx = max(etf_scenario.lookback + 1, 130)
    stock_ready_idx = _stock_ready_index(
        start_idx=start_idx,
        raw_ready_idx=253,
        etf_rebalance_freq=scenario.etf_rebalance_freq,
        align_to_etf=align_stock_rebalance_to_etf,
    )
    for i in range(start_idx, len(common_index)):
        date = pd.Timestamp(common_index[i])
        current_prices = valuation_prices.iloc[i]
        previous_prices = valuation_prices.iloc[i - 1]
        market_pnl = {"etf_core": 0.0, "crisis_gold": 0.0, "stock_alpha": 0.0}
        for symbol, shares in positions.items():
            current_price = float(current_prices.get(symbol, np.nan))
            previous_price = float(previous_prices.get(symbol, np.nan))
            if not math.isfinite(current_price) or not math.isfinite(previous_price):
                continue
            group = _group_for_symbol(symbol, stock_symbol_set, active_crisis_symbols)
            market_pnl[group] += shares * (current_price - previous_price)
        nav_before = prev_nav + sum(market_pnl.values())
        if nav_before <= 0:
            raise RuntimeError(f"joint NAV became non-positive on {date.date()}")
        fee_by_group = {"etf_core": 0.0, "crisis_gold": 0.0, "stock_alpha": 0.0}

        etf_rebalance = (i - start_idx) % scenario.etf_rebalance_freq == 0
        stock_rebalance = (
            scenario.stock_alpha_cap > 1e-12
            and i >= stock_ready_idx
            and (i - stock_ready_idx) % scenario.stock_rebalance_freq == 0
        )
        if etf_rebalance or stock_rebalance:
            signal_idx = i - 1
            regime, _regime_diag = _market_regime(etf_close, signal_idx, etf_roles)
            if etf_rebalance:
                etf_score = _score_etfs(etf_close, signal_idx, etf_scenario, etf_roles)
                current_etf_weights, _etf_diag = _target_etf_weights(
                    etf_close,
                    signal_idx=signal_idx,
                    score=etf_score,
                    scenario=etf_scenario,
                    nav=nav_before,
                    peak_nav=peak_nav,
                    roles=etf_roles,
                )
                if crisis_allocator is None:
                    crisis_weight, _crisis_diag = _crisis_target_weight(
                        etf_close,
                        signal_idx=signal_idx,
                        regime=regime,
                        crisis_cap=scenario.crisis_cap,
                    )
                    current_crisis_weights = (
                        {GOLD_SYMBOL: crisis_weight} if crisis_weight > 1e-12 else {}
                    )
                else:
                    current_crisis_weights, _crisis_diag = crisis_allocator(
                        crisis_close,
                        signal_idx=signal_idx,
                        regime=regime,
                        crisis_cap=scenario.crisis_cap,
                    )
            if i >= stock_ready_idx:
                current_stock_budget, _stock_budget_diag = _stock_target_budget(
                    stock_gate_returns,
                    signal_date=pd.Timestamp(common_index[signal_idx]),
                    regime=regime,
                    stock_alpha_cap=scenario.stock_alpha_cap,
                    nav=nav_before,
                    peak_nav=peak_nav,
                    lookback=scenario.stock_lookback,
                )
            else:
                current_stock_budget = 0.0
            if stock_rebalance:
                stock_score = _score_for_mode(
                    rank_mode=scenario.stock_rank_mode,
                    factors=factors,
                    rolling_ic=rolling_ic,
                    signal_idx=signal_idx,
                )
                current_stock_symbols = _select_small_account_targets(
                    score=stock_score,
                    industry_map=industry_map,
                    trade_prices=stock_close.iloc[i],
                    nav=nav_before,
                    stock_gross=current_stock_budget,
                    scenario=stock_select_scenario,
                )
            target_weights, target_diag = _merge_target_weights(
                etf_weights=current_etf_weights,
                stock_symbols=current_stock_symbols,
                stock_budget=current_stock_budget,
                crisis_weights=current_crisis_weights,
            )
            trade_prices = pd.concat([execution_etf.iloc[i], stock_close.iloc[i]])
            positions, cash, execution_diag = _execute_joint_rebalance(
                positions=positions,
                cash=cash,
                target_weights=target_weights,
                nav=nav_before,
                prices=trade_prices,
                trade_date=date,
                stock_symbols=stock_symbol_set,
                trading_constraints=trading_constraints,
                stock_cost=stock_cost,
                etf_cost=etf_cost,
                lot_size=scenario.lot_size,
                crisis_symbols=active_crisis_symbols,
            )
            for key in (
                "orders",
                "buy_orders",
                "sell_orders",
                "buy_notional",
                "sell_notional",
                "fees",
                "blocked_buys",
                "blocked_sells",
                "lot_blocked_targets",
                "cash_blocked_buys",
                "missing_price_blocks",
            ):
                diagnostics[key] += execution_diag[key]
            for group, fee in execution_diag["fees_by_group"].items():
                diagnostics["fees_by_group"][group] += fee
                fee_by_group[group] += fee
            target_rows.append(target_diag)

        nav_after = cash
        group_values = {"etf_core": 0.0, "crisis_gold": 0.0, "stock_alpha": 0.0}
        for symbol, shares in positions.items():
            price = float(current_prices.get(symbol, np.nan))
            if math.isfinite(price) and price > 0:
                value = shares * price
                nav_after += value
                group = _group_for_symbol(symbol, stock_symbol_set, active_crisis_symbols)
                group_values[group] += value
        expected_nav = nav_before - sum(fee_by_group.values())
        invariant_error = abs(nav_after - expected_nav)
        invariant_max_error = max(invariant_max_error, invariant_error)
        if invariant_error > max(0.01, nav_after * 1e-8):
            raise RuntimeError(f"joint equity invariant failed on {date.date()}: {invariant_error}")
        daily_return = nav_after / prev_nav - 1.0
        contribution = {
            group: (market_pnl[group] - fee_by_group[group]) / prev_nav for group in market_pnl
        }
        contribution["reconciliation_residual"] = daily_return - sum(contribution.values())
        actual_exposure = sum(group_values.values()) / nav_after if nav_after > 0 else 0.0
        risk_capacity_violations += int(actual_exposure > RISK_GATE_LIMIT + 1e-9)
        returns.append(float(daily_return))
        dates.append(date)
        attr_rows.append(contribution)
        exposure_values.append(float(actual_exposure))
        cash_weights.append(float(cash / nav_after) if nav_after > 0 else 0.0)
        stock_holdings.append(sum(1 for symbol in positions if symbol in stock_symbol_set))
        prev_nav = nav_after
        peak_nav = max(peak_nav, nav_after)

    return_series = pd.Series(returns, index=pd.DatetimeIndex(dates), dtype=float, name="v38_joint")
    attribution = pd.DataFrame(attr_rows, index=return_series.index)
    full = _metrics(return_series, final_nav=prev_nav, initial_capital=scenario.capital)
    full.update(
        {
            "avg_actual_risk_exposure": round(float(np.mean(exposure_values)), 4),
            "max_actual_risk_exposure": round(float(max(exposure_values)), 4),
            "avg_cash_weight": round(float(np.mean(cash_weights)), 4),
            "avg_stock_holdings": round(float(np.mean(stock_holdings)), 2),
            "max_stock_holdings": int(max(stock_holdings)),
            "total_fees": round(float(diagnostics["fees"]), 2),
            "fees_pct_initial_capital": round(float(diagnostics["fees"] / scenario.capital), 4),
            "orders": int(diagnostics["orders"]),
            "blocked_buys": int(diagnostics["blocked_buys"]),
            "blocked_sells": int(diagnostics["blocked_sells"]),
            "cash_blocked_buys": int(diagnostics["cash_blocked_buys"]),
            "lot_blocked_targets": int(diagnostics["lot_blocked_targets"]),
            "invariant_max_error": round(float(invariant_max_error), 10),
            "risk_capacity_violations": int(risk_capacity_violations),
            "avg_target_etf": round(float(np.mean([row["etf_target"] for row in target_rows])), 4),
            "avg_target_stock": round(
                float(np.mean([row["stock_target"] for row in target_rows])), 4
            ),
            "avg_target_crisis": round(
                float(np.mean([row["crisis_target"] for row in target_rows])), 4
            ),
        }
    )
    wf = _walk_forward_from_returns(return_series) if len(return_series) > 2100 else {}
    result: dict[str, Any] = {
        "name": (
            f"v38_joint_{int(scenario.capital)}_stock{int(scenario.stock_alpha_cap * 100)}_"
            f"crisis{int(scenario.crisis_cap * 100)}"
        ),
        "scenario": asdict(scenario),
        "research_only": True,
        "production_ready": False,
        "full": full,
        "wf": wf,
        "weakest_fold_attribution": _weakest_fold_attribution(return_series, attribution, wf),
        "diagnostics": diagnostics,
    }
    if include_series:
        result["_return_series"] = return_series
        result["_attribution"] = attribution
    return result


def _evaluate_improvement(row: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    full = row["full"]
    base_full = baseline["full"]
    wf = row["wf"]
    base_wf = baseline["wf"]
    annual_delta = float(full["annual_return"]) - float(base_full["annual_return"])
    sharpe_delta = float(full["sharpe_ratio"]) - float(base_full["sharpe_ratio"])
    drawdown_delta = float(full["max_drawdown"]) - float(base_full["max_drawdown"])
    oos_delta = float(wf.get("avg_oos_sharpe", -999)) - float(base_wf.get("avg_oos_sharpe", -999))
    min_oos = _min_oos(wf)
    base_min_oos = _min_oos(base_wf)
    min_oos_delta = (
        float(min_oos) - float(base_min_oos)
        if min_oos is not None and base_min_oos is not None
        else -999.0
    )
    invariant_gate = (
        float(full.get("invariant_max_error", 999)) <= 0.01
        and int(full.get("risk_capacity_violations", 1)) == 0
        and float(full.get("max_actual_risk_exposure", 999)) <= RISK_GATE_LIMIT
    )
    relative_gate = (
        annual_delta >= 0.005
        and sharpe_delta >= 0.0
        and float(full["max_drawdown"]) >= -0.15
        and drawdown_delta >= -0.02
        and oos_delta >= 0.0
        and min_oos_delta >= 0.0
        and invariant_gate
    )
    minimum_gate = (
        float(full["sharpe_ratio"]) >= 1.2
        and float(full["max_drawdown"]) >= -0.15
        and float(full["win_rate"]) >= 0.40
        and (min_oos is not None and min_oos >= 0.0)
        and invariant_gate
    )
    return {
        "annual_return_delta": round(annual_delta, 4),
        "sharpe_delta": round(sharpe_delta, 4),
        "max_drawdown_delta": round(drawdown_delta, 4),
        "avg_oos_sharpe_delta": round(oos_delta, 4),
        "min_oos_sharpe_delta": round(min_oos_delta, 4),
        "joint_invariant_gate_passes": bool(invariant_gate),
        "relative_improvement_gate_passes": bool(relative_gate),
        "production_minimum_gate_passes": bool(minimum_gate),
    }


def _history_audit(
    benchmark_metadata: dict[str, Any], hedge_dir: Path, min_days: int
) -> dict[str, Any]:
    benchmark_assets = {
        symbol: metadata
        for symbol, metadata in benchmark_metadata.items()
        if metadata.get("exists") and not metadata.get("failure")
    }
    hedge_assets: dict[str, Any] = {}
    for path in sorted(hedge_dir.glob("*.parquet")):
        if path.name == "hedge_crisis_assets.parquet":
            continue
        frame = pd.read_parquet(path, columns=["close"])
        index = pd.to_datetime(frame.index, errors="coerce")
        hedge_assets[path.stem] = {
            "path": str(path),
            "rows": int(len(frame)),
            "start": index.min().date().isoformat(),
            "end": index.max().date().isoformat(),
            "long_history_ready": bool(len(frame) >= min_days),
        }
    return {
        "long_history_min_days": int(min_days),
        "benchmark_assets": benchmark_assets,
        "hedge_assets": hedge_assets,
        "eligible_long_history_crisis_assets": [GOLD_SYMBOL],
        "short_history_assets_rejected": sorted(
            symbol for symbol, item in hedge_assets.items() if not item["long_history_ready"]
        ),
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "name",
        "capital",
        "stock_alpha_cap",
        "crisis_cap",
        "relative_improvement_gate_passes",
        "production_minimum_gate_passes",
        "sharpe_ratio",
        "annual_return",
        "max_drawdown",
        "win_rate",
        "avg_oos_sharpe",
        "min_oos_sharpe",
        "annual_return_delta",
        "sharpe_delta",
        "max_drawdown_delta",
        "avg_target_stock",
        "avg_target_crisis",
        "max_actual_risk_exposure",
        "orders",
        "total_fees",
        "weakest_fold",
        "weakest_fold_oos",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            full = row["full"]
            gates = row["gates"]
            weak = row.get("weakest_fold_attribution", {})
            writer.writerow(
                {
                    "name": row["name"],
                    "capital": row["scenario"]["capital"],
                    "stock_alpha_cap": row["scenario"]["stock_alpha_cap"],
                    "crisis_cap": row["scenario"]["crisis_cap"],
                    "relative_improvement_gate_passes": gates["relative_improvement_gate_passes"],
                    "production_minimum_gate_passes": gates["production_minimum_gate_passes"],
                    "sharpe_ratio": full.get("sharpe_ratio"),
                    "annual_return": full.get("annual_return"),
                    "max_drawdown": full.get("max_drawdown"),
                    "win_rate": full.get("win_rate"),
                    "avg_oos_sharpe": row["wf"].get("avg_oos_sharpe"),
                    "min_oos_sharpe": _min_oos(row["wf"]),
                    "annual_return_delta": gates["annual_return_delta"],
                    "sharpe_delta": gates["sharpe_delta"],
                    "max_drawdown_delta": gates["max_drawdown_delta"],
                    "avg_target_stock": full.get("avg_target_stock"),
                    "avg_target_crisis": full.get("avg_target_crisis"),
                    "max_actual_risk_exposure": full.get("max_actual_risk_exposure"),
                    "orders": full.get("orders"),
                    "total_fees": full.get("total_fees"),
                    "weakest_fold": weak.get("fold"),
                    "weakest_fold_oos": weak.get("oos_sharpe"),
                }
            )


def main() -> None:
    started = time.time()
    args = _parse_args()
    capitals = _parse_float_list(args.capitals)
    stock_caps = _parse_float_list(args.stock_alpha_caps)
    crisis_caps = _parse_float_list(args.crisis_caps)
    if any(value <= 0.0 or value >= 0.50 for value in stock_caps):
        raise ValueError("stock alpha caps must be between zero and 0.50")
    if any(value < 0.0 or value > 0.30 for value in crisis_caps):
        raise ValueError("crisis caps must be between zero and 0.30")
    benchmark_dir = Path(args.benchmark_dir)
    hedge_dir = Path(args.hedge_asset_dir)
    universe_path = Path(args.universe)
    trading_status_path = Path(args.trading_status_path)
    output_json = Path(args.output_json)
    output_csv = Path(args.output_csv)

    etf_close, etf_metadata, etf_roles = _load_multi_asset_panel(
        benchmark_dir,
        hedge_dir,
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
    common_index = pd.DatetimeIndex(etf_close.index).intersection(stock_close.index).sort_values()
    etf_close = etf_close.reindex(common_index)
    stock_close = stock_close.reindex(common_index)
    stock_volume = stock_volume.reindex(index=common_index, columns=stock_close.columns)
    stock_amount = stock_amount.reindex(index=common_index, columns=stock_close.columns)
    valuation_stock_close = stock_close.ffill()
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
        raise RuntimeError("V38 requires V31 trading-status constraints")

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

    gate_return_cache: dict[tuple[float, float], pd.Series] = {}
    for capital in capitals:
        for stock_cap in stock_caps:
            gate_scenario = ScenarioConfig(
                capital=capital * stock_cap,
                max_positions=2,
                rank_mode="blend",
                gross_profile="small_balanced",
                rebalance_freq=40,
                max_per_industry=1,
                candidate_pool_multiplier=10,
                per_position_budget_buffer=1.50,
            )
            gate_result = _run_stock_scenario(
                scenario=gate_scenario,
                close=stock_close,
                valuation_close=valuation_stock_close,
                factors=factors,
                rolling_ic=rolling_ic,
                regime=stock_regime,
                industry_map=industry_map,
                trading_constraints=trading_constraints,
                cost=stock_cost,
                include_return_series=True,
            )
            gate_return_cache[(capital, stock_cap)] = gate_result["_return_series"]

    baseline_by_capital: dict[float, dict[str, Any]] = {}
    baseline_parity_by_capital: dict[str, dict[str, Any]] = {}
    for capital in capitals:
        baseline = _run_joint_scenario(
            scenario=JointScenario(capital=capital, stock_alpha_cap=0.0, crisis_cap=0.0),
            etf_close=etf_close,
            etf_roles=etf_roles,
            stock_close=stock_close,
            valuation_stock_close=valuation_stock_close,
            factors=factors,
            rolling_ic=rolling_ic,
            industry_map=industry_map,
            trading_constraints=trading_constraints,
            stock_gate_returns=pd.Series(0.0, index=common_index),
            stock_cost=stock_cost,
            etf_cost=etf_cost,
            include_series=True,
        )
        reference = _run_v36_scenario(
            etf_close,
            RegimeBudgetScenario(
                capital=capital,
                universe_mode="core_long",
                max_assets=1,
                rebalance_freq=20,
                rank_mode="risk_adjusted",
                risk_profile="aggressive",
            ),
            etf_cost,
            etf_roles,
            etf_coverage,
            include_return_series=True,
        )
        baseline_parity_by_capital[str(int(capital))] = _compare_baseline_returns(
            baseline.pop("_return_series"), reference.pop("_return_series")
        )
        baseline.pop("_attribution")
        baseline_by_capital[capital] = baseline

    rows: list[dict[str, Any]] = []
    for capital in capitals:
        baseline = baseline_by_capital[capital]
        for stock_cap in stock_caps:
            for crisis_cap in crisis_caps:
                row = _run_joint_scenario(
                    scenario=JointScenario(
                        capital=capital,
                        stock_alpha_cap=stock_cap,
                        crisis_cap=crisis_cap,
                    ),
                    etf_close=etf_close,
                    etf_roles=etf_roles,
                    stock_close=stock_close,
                    valuation_stock_close=valuation_stock_close,
                    factors=factors,
                    rolling_ic=rolling_ic,
                    industry_map=industry_map,
                    trading_constraints=trading_constraints,
                    stock_gate_returns=gate_return_cache[(capital, stock_cap)],
                    stock_cost=stock_cost,
                    etf_cost=etf_cost,
                )
                row["baseline_full"] = baseline["full"]
                row["baseline_wf"] = baseline["wf"]
                row["gates"] = _evaluate_improvement(row, baseline)
                rows.append(row)

    rows.sort(
        key=lambda row: (
            bool(row["gates"]["relative_improvement_gate_passes"]),
            float(row["gates"]["annual_return_delta"]),
            float(row["full"]["sharpe_ratio"]),
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
        "version": "V38-joint-account-crisis-alpha-v1",
        "research_only": True,
        "production_ready": False,
        "objective": "joint-account V36 ETF, executable stock alpha and long-history gold crisis sleeve",
        "methodology": {
            "execution": "single shared cash account; all sells precede buys",
            "signal_timing": "prior-day or older inputs only",
            "risk_capacity": TARGET_RISK_CAPACITY,
            "risk_gate_limit": RISK_GATE_LIMIT,
            "crisis_asset": GOLD_SYMBOL,
            "short_history_assets": "audited but excluded from long-history results",
        },
        "universe": str(universe_path),
        "universe_sha256": _sha256_file(universe_path),
        "coverage": {
            "stock_symbols": int(stock_close.shape[1]),
            "dates": int(len(common_index)),
            "start": common_index.min().date().isoformat(),
            "end": common_index.max().date().isoformat(),
            "industry_overlap": int(
                sum(1 for symbol in stock_close.columns if symbol in industry_map)
            ),
            "etf": etf_coverage,
        },
        "history_audit": _history_audit(etf_metadata, hedge_dir, int(args.long_history_min_days)),
        "trading_constraint_summary": trading_summary,
        "cost_model": {"stock": asdict(stock_cost), "etf": asdict(etf_cost)},
        "scenario_count": len(rows),
        "v36_baseline_parity_passes": bool(
            baseline_parity_by_capital
            and all(item["passes"] for item in baseline_parity_by_capital.values())
        ),
        "v36_baseline_parity_by_capital": baseline_parity_by_capital,
        "relative_improvement_pass_count": int(
            sum(1 for row in rows if row["gates"]["relative_improvement_gate_passes"])
        ),
        "production_minimum_pass_count": int(
            sum(1 for row in rows if row["gates"]["production_minimum_gate_passes"])
        ),
        "baseline_by_capital": baseline_by_capital,
        "best_by_capital": best_by_capital,
        "results": rows,
        "production_blockers": [
            "V38 is local research evidence and does not approve live trading",
            "only gold has sufficient local crisis-asset history; bonds, commodities and global assets remain short-history",
            "V31 trading status is free-source approximation rather than vendor PIT evidence",
            "requires broker order/fill replay, 90-day paper trading and exchange position reconciliation",
            "requires external WORM/Secret/Approval/Provider/Capacity/DR evidence gate",
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
                "scenario_count": report["scenario_count"],
                "relative_improvement_pass_count": report["relative_improvement_pass_count"],
                "production_minimum_pass_count": report["production_minimum_pass_count"],
                "v36_baseline_parity_passes": report["v36_baseline_parity_passes"],
                "best_by_capital": {
                    capital: {
                        "name": row["name"],
                        "sharpe": row["full"]["sharpe_ratio"],
                        "annual_return": row["full"]["annual_return"],
                        "max_drawdown": row["full"]["max_drawdown"],
                        "min_oos": _min_oos(row["wf"]),
                        "weakest_fold": row["weakest_fold_attribution"],
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
