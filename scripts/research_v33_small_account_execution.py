#!/usr/bin/env python3
"""Research V33: small-account executable A-share simulation.

This script answers a narrow research question: whether the V29/V32 price-only
idea still makes sense for RMB 50k/100k accounts after 100-share lots, minimum
commission, cash drag and V31 tradability constraints. It is not production
approval and deliberately emits ``production_ready=false``.
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
from _paths import RESULTS_DIR
from research_v28_factor_direction import (
    DEFAULT_UNIVERSE,
    PRICE_FACTOR_NAMES,
    _build_factors,
    _build_market_regime_features,
    _industry_demean,
    _load_industry_map,
    _load_market_panel,
    _market_regime,
    _read_codes,
    _rolling_ic_weights,
    _score_ic_adaptive,
    _score_static,
)
from research_v29_portfolio_layer import (
    DEFAULT_V31_TRADING_STATUS,
    _load_trading_status_constraints,
    _walk_forward_from_returns,
)

DEFAULT_OUTPUT_JSON = RESULTS_DIR / "quant_v33_small_account_execution.json"
DEFAULT_OUTPUT_CSV = RESULTS_DIR / "quant_v33_small_account_execution.csv"
LONGHORIZON_GUARD = {
    "base_gross": 0.60,
    "bull_gross": 0.87,
    "neutral_gross": 0.60,
    "risk_off_gross": 0.16,
    "vol_target": 0.115,
    "dd_reduce_threshold": 0.05,
    "dd_reduce_scale": 0.35,
    "dd_stop_threshold": 0.09,
    "dd_stop_scale": 0.04,
}
GROSS_PROFILES = {
    "v32_guard": LONGHORIZON_GUARD,
    "small_defensive": {
        "base_gross": 0.50,
        "bull_gross": 0.76,
        "neutral_gross": 0.50,
        "risk_off_gross": 0.20,
        "vol_target": 0.14,
        "dd_reduce_threshold": 0.07,
        "dd_reduce_scale": 0.45,
        "dd_stop_threshold": 0.13,
        "dd_stop_scale": 0.10,
    },
    "small_balanced": {
        "base_gross": 0.62,
        "bull_gross": 0.88,
        "neutral_gross": 0.62,
        "risk_off_gross": 0.25,
        "vol_target": 0.17,
        "dd_reduce_threshold": 0.08,
        "dd_reduce_scale": 0.55,
        "dd_stop_threshold": 0.16,
        "dd_stop_scale": 0.20,
    },
}


@dataclass(frozen=True)
class CostConfig:
    commission_rate: float = 0.00025
    min_commission: float = 5.0
    stamp_duty_rate: float = 0.0005
    slippage_bps: float = 5.0


@dataclass(frozen=True)
class ScenarioConfig:
    capital: float
    max_positions: int
    rank_mode: Literal["ic", "lowvol_reversal", "defensive", "blend"]
    gross_profile: Literal["v32_guard", "small_defensive", "small_balanced"] = "v32_guard"
    rebalance_freq: int = 10
    lot_size: int = 100
    candidate_pool_multiplier: int = 6
    max_per_industry: int = 3
    per_position_budget_buffer: float = 1.20


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument("--trading-status-path", default=str(DEFAULT_V31_TRADING_STATUS))
    parser.add_argument("--start-date", default="20060101")
    parser.add_argument("--end-date", default="20260529")
    parser.add_argument("--min-history", type=int, default=252)
    parser.add_argument("--capitals", default="50000,100000")
    parser.add_argument("--max-positions", default="5,8,10,12")
    parser.add_argument("--rank-modes", default="ic,blend,lowvol_reversal,defensive")
    parser.add_argument("--profiles", default="v32_guard,small_defensive,small_balanced")
    parser.add_argument("--commission-rate", type=float, default=0.00025)
    parser.add_argument("--min-commission", type=float, default=5.0)
    parser.add_argument("--stamp-duty-rate", type=float, default=0.0005)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    return parser.parse_args()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _round_down_to_lot(shares: float, lot_size: int) -> int:
    if lot_size <= 0:
        raise ValueError("lot_size must be positive")
    if shares <= 0 or not math.isfinite(shares):
        return 0
    return int(math.floor(shares / lot_size) * lot_size)


def _order_fee(notional: float, side: Literal["buy", "sell"], cost: CostConfig) -> float:
    if notional <= 0 or not math.isfinite(notional):
        return 0.0
    commission = max(float(notional) * cost.commission_rate, cost.min_commission)
    slippage = float(notional) * cost.slippage_bps / 10000.0
    stamp = float(notional) * cost.stamp_duty_rate if side == "sell" else 0.0
    return float(commission + slippage + stamp)


def _affordable_buy_shares(
    *,
    cash: float,
    price: float,
    desired_shares: int,
    lot_size: int,
    cost: CostConfig,
) -> int:
    if cash <= 0 or price <= 0 or desired_shares <= 0:
        return 0
    shares = _round_down_to_lot(desired_shares, lot_size)
    while shares > 0:
        notional = float(shares) * price
        if notional + _order_fee(notional, "buy", cost) <= cash + 1e-9:
            return shares
        shares -= lot_size
    return 0


def _metrics(returns: pd.Series, *, final_nav: float, initial_capital: float) -> dict[str, Any]:
    series = pd.to_numeric(returns, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if len(series) < 2:
        return {"error": "not enough returns", "n_days": int(len(series))}
    equity = (1.0 + series).cumprod()
    ann_ret = float(series.mean() * 252)
    ann_vol = float(series.std(ddof=1) * np.sqrt(252))
    sharpe = (ann_ret - 0.025) / ann_vol if ann_vol > 1e-8 else 0.0
    peak = equity.cummax()
    mdd = float(((equity - peak) / peak).min())
    return {
        "sharpe_ratio": round(float(sharpe), 4),
        "annual_return": round(ann_ret, 4),
        "annual_volatility": round(ann_vol, 4),
        "max_drawdown": round(mdd, 4),
        "win_rate": round(float((series > 0).mean()), 4),
        "total_return": round(float(final_nav / initial_capital - 1.0), 4),
        "final_nav": round(float(final_nav), 2),
        "n_days": int(len(series)),
    }


def _rank_zscore(score: pd.Series) -> pd.Series:
    clean = score.replace([np.inf, -np.inf], np.nan).dropna()
    if len(clean) < 20:
        return pd.Series(dtype=float)
    ranks = clean.rank(pct=True)
    std = float(ranks.std(ddof=1))
    if std <= 1e-12:
        return pd.Series(dtype=float)
    return (ranks - float(ranks.mean())) / std


def _score_for_mode(
    *,
    rank_mode: str,
    factors: dict[str, pd.DataFrame],
    rolling_ic: pd.DataFrame,
    signal_idx: int,
) -> pd.Series:
    lowvol_reversal_weights = {
        "reversal_5d": 0.20,
        "momentum_20d": -0.20,
        "momentum_60d": -0.20,
        "low_vol_20d": 0.25,
        "low_vol_60d": 0.20,
        "liquidity_20d": -0.10,
        "volume_stability_20d": 0.15,
    }
    defensive_weights = {
        "low_vol_20d": 0.35,
        "low_vol_60d": 0.25,
        "reversal_5d": 0.15,
        "volume_stability_20d": 0.15,
        "liquidity_20d": -0.10,
    }
    if rank_mode == "ic":
        return _score_ic_adaptive(factors, rolling_ic, signal_idx, factor_names=PRICE_FACTOR_NAMES)
    if rank_mode == "lowvol_reversal":
        return _score_static(factors, signal_idx, lowvol_reversal_weights)
    if rank_mode == "defensive":
        return _score_static(factors, signal_idx, defensive_weights)
    if rank_mode != "blend":
        raise ValueError(f"unknown rank_mode: {rank_mode}")
    parts = [
        (0.40, _score_ic_adaptive(factors, rolling_ic, signal_idx, factor_names=PRICE_FACTOR_NAMES)),
        (0.35, _score_static(factors, signal_idx, lowvol_reversal_weights)),
        (0.25, _score_static(factors, signal_idx, defensive_weights)),
    ]
    combined: pd.Series | None = None
    for weight, score in parts:
        normalized = _rank_zscore(score)
        if normalized.empty:
            continue
        combined = normalized * weight if combined is None else combined.add(normalized * weight, fill_value=0.0)
    return combined.dropna() if combined is not None else pd.Series(dtype=float)


def _stock_gross_for_day(
    *,
    gross_profile: str,
    regime: dict[str, np.ndarray],
    signal_idx: int,
    equity: float,
    peak_equity: float,
) -> tuple[float, bool]:
    config = GROSS_PROFILES[gross_profile]
    gross, risk_off, market_vol = _market_regime(regime, signal_idx, config)
    gross = min(float(config["base_gross"]), gross) if risk_off else gross
    if market_vol > 1e-8:
        gross *= min(1.0, float(config["vol_target"]) / market_vol)
    current_dd = (equity - peak_equity) / peak_equity if peak_equity > 0 else 0.0
    if current_dd < -float(config["dd_stop_threshold"]):
        gross *= float(config["dd_stop_scale"])
    elif current_dd < -float(config["dd_reduce_threshold"]):
        gross *= float(config["dd_reduce_scale"])
    return max(0.0, min(1.0, float(gross))), bool(risk_off)


def _select_small_account_targets(
    *,
    score: pd.Series,
    industry_map: dict[str, str],
    trade_prices: pd.Series,
    nav: float,
    stock_gross: float,
    scenario: ScenarioConfig,
) -> list[str]:
    if score.empty or stock_gross <= 0.0:
        return []
    adjusted = _industry_demean(score, industry_map).replace([np.inf, -np.inf], np.nan).dropna()
    if adjusted.empty:
        return []
    per_position_budget = nav * stock_gross / max(1, scenario.max_positions)
    max_one_lot_value = per_position_budget * scenario.per_position_budget_buffer
    selected: list[str] = []
    industry_counts: dict[str, int] = {}
    ranked = adjusted.sort_values(ascending=False)
    for symbol_value in ranked.index[: scenario.max_positions * scenario.candidate_pool_multiplier]:
        symbol = str(symbol_value)
        price = float(trade_prices.get(symbol, np.nan))
        if not math.isfinite(price) or price <= 0:
            continue
        if price * scenario.lot_size > max_one_lot_value:
            continue
        industry = industry_map.get(symbol, "__UNKNOWN__")
        if industry_counts.get(industry, 0) >= scenario.max_per_industry:
            continue
        selected.append(symbol)
        industry_counts[industry] = industry_counts.get(industry, 0) + 1
        if len(selected) >= scenario.max_positions:
            break
    return selected


def _status_value(
    trading_constraints: dict[str, pd.DataFrame],
    key: str,
    date: pd.Timestamp,
    symbol: str,
    *,
    default: bool,
) -> bool:
    frame = trading_constraints[key]
    if date not in frame.index or symbol not in frame.columns:
        return default
    return bool(frame.at[date, symbol])


def _execute_rebalance(
    *,
    positions: dict[str, int],
    cash: float,
    target_symbols: list[str],
    target_gross: float,
    nav: float,
    trade_prices: pd.Series,
    trade_date: pd.Timestamp,
    trading_constraints: dict[str, pd.DataFrame],
    scenario: ScenarioConfig,
    cost: CostConfig,
) -> tuple[dict[str, int], float, dict[str, float]]:
    target_shares: dict[str, int] = {}
    per_name_value = nav * target_gross / max(1, len(target_symbols)) if target_symbols else 0.0
    lot_blocked_targets = 0
    for symbol in target_symbols:
        price = float(trade_prices.get(symbol, np.nan))
        shares = _round_down_to_lot(per_name_value / price, scenario.lot_size) if price > 0 else 0
        if shares <= 0:
            lot_blocked_targets += 1
            continue
        target_shares[symbol] = shares

    updated = {symbol: int(shares) for symbol, shares in positions.items() if shares > 0}
    diagnostics = {
        "orders": 0.0,
        "buy_orders": 0.0,
        "sell_orders": 0.0,
        "buy_notional": 0.0,
        "sell_notional": 0.0,
        "fees": 0.0,
        "blocked_buys": 0.0,
        "blocked_sells": 0.0,
        "lot_blocked_targets": float(lot_blocked_targets),
        "cash_blocked_buys": 0.0,
    }

    for symbol, shares in list(updated.items()):
        desired = int(target_shares.get(symbol, 0))
        sell_shares = shares - desired
        if sell_shares <= 0:
            continue
        price = float(trade_prices.get(symbol, np.nan))
        tradable = _status_value(trading_constraints, "is_tradable", trade_date, symbol, default=False)
        limit_down = _status_value(trading_constraints, "is_limit_down", trade_date, symbol, default=True)
        if not math.isfinite(price) or price <= 0 or not tradable or limit_down:
            diagnostics["blocked_sells"] += 1.0
            continue
        notional = float(sell_shares) * price
        fee = _order_fee(notional, "sell", cost)
        cash += notional - fee
        diagnostics["orders"] += 1.0
        diagnostics["sell_orders"] += 1.0
        diagnostics["sell_notional"] += notional
        diagnostics["fees"] += fee
        remaining = shares - sell_shares
        if remaining > 0:
            updated[symbol] = remaining
        else:
            updated.pop(symbol, None)

    for symbol, desired in target_shares.items():
        current = int(updated.get(symbol, 0))
        buy_shares = desired - current
        if buy_shares <= 0:
            continue
        price = float(trade_prices.get(symbol, np.nan))
        tradable = _status_value(trading_constraints, "is_tradable", trade_date, symbol, default=False)
        limit_up = _status_value(trading_constraints, "is_limit_up", trade_date, symbol, default=True)
        if not math.isfinite(price) or price <= 0 or not tradable or limit_up:
            diagnostics["blocked_buys"] += 1.0
            continue
        affordable = _affordable_buy_shares(
            cash=cash,
            price=price,
            desired_shares=buy_shares,
            lot_size=scenario.lot_size,
            cost=cost,
        )
        if affordable <= 0:
            diagnostics["cash_blocked_buys"] += 1.0
            continue
        notional = float(affordable) * price
        fee = _order_fee(notional, "buy", cost)
        cash -= notional + fee
        updated[symbol] = current + affordable
        diagnostics["orders"] += 1.0
        diagnostics["buy_orders"] += 1.0
        diagnostics["buy_notional"] += notional
        diagnostics["fees"] += fee

    return {symbol: shares for symbol, shares in updated.items() if shares > 0}, float(cash), diagnostics


def _portfolio_value(
    *,
    positions: dict[str, int],
    cash: float,
    prices: pd.Series,
) -> tuple[float, float]:
    stock_value = 0.0
    for symbol, shares in positions.items():
        price = float(prices.get(symbol, np.nan))
        if math.isfinite(price) and price > 0:
            stock_value += float(shares) * price
    return float(cash + stock_value), float(stock_value)


def _run_scenario(
    *,
    scenario: ScenarioConfig,
    close: pd.DataFrame,
    valuation_close: pd.DataFrame,
    factors: dict[str, pd.DataFrame],
    rolling_ic: pd.DataFrame,
    regime: dict[str, np.ndarray],
    industry_map: dict[str, str],
    trading_constraints: dict[str, pd.DataFrame],
    cost: CostConfig,
) -> dict[str, Any]:
    positions: dict[str, int] = {}
    cash = float(scenario.capital)
    prev_nav = float(scenario.capital)
    peak_nav = float(scenario.capital)
    returns: list[float] = []
    dates: list[pd.Timestamp] = []
    holdings_counts: list[int] = []
    stock_exposures: list[float] = []
    cash_weights: list[float] = []
    target_gross_values: list[float] = []
    risk_off_days = 0
    total_diag = {
        "orders": 0.0,
        "buy_orders": 0.0,
        "sell_orders": 0.0,
        "buy_notional": 0.0,
        "sell_notional": 0.0,
        "fees": 0.0,
        "blocked_buys": 0.0,
        "blocked_sells": 0.0,
        "lot_blocked_targets": 0.0,
        "cash_blocked_buys": 0.0,
    }
    start_idx = 253
    for i in range(start_idx, len(close)):
        trade_date = pd.Timestamp(close.index[i])
        nav_before, stock_value_before = _portfolio_value(
            positions=positions,
            cash=cash,
            prices=valuation_close.iloc[i],
        )
        if nav_before <= 0:
            raise RuntimeError(f"NAV became non-positive on {trade_date.date()}: {nav_before}")

        if (i - start_idx) % scenario.rebalance_freq == 0:
            signal_idx = i - 1
            gross, risk_off = _stock_gross_for_day(
                gross_profile=scenario.gross_profile,
                regime=regime,
                signal_idx=signal_idx,
                equity=nav_before,
                peak_equity=peak_nav,
            )
            score = _score_for_mode(
                rank_mode=scenario.rank_mode,
                factors=factors,
                rolling_ic=rolling_ic,
                signal_idx=signal_idx,
            )
            target_symbols = _select_small_account_targets(
                score=score,
                industry_map=industry_map,
                trade_prices=close.iloc[i],
                nav=nav_before,
                stock_gross=gross,
                scenario=scenario,
            )
            positions, cash, diag = _execute_rebalance(
                positions=positions,
                cash=cash,
                target_symbols=target_symbols,
                target_gross=gross,
                nav=nav_before,
                trade_prices=close.iloc[i],
                trade_date=trade_date,
                trading_constraints=trading_constraints,
                scenario=scenario,
                cost=cost,
            )
            for key, value in diag.items():
                total_diag[key] += float(value)
            risk_off_days += int(risk_off)
            target_gross_values.append(float(gross))

        nav_after, stock_value_after = _portfolio_value(
            positions=positions,
            cash=cash,
            prices=valuation_close.iloc[i],
        )
        returns.append(float(nav_after / prev_nav - 1.0))
        dates.append(trade_date)
        prev_nav = nav_after
        peak_nav = max(peak_nav, nav_after)
        holdings_counts.append(len(positions))
        stock_exposures.append(float(stock_value_after / nav_after) if nav_after > 0 else 0.0)
        cash_weights.append(float(cash / nav_after) if nav_after > 0 else 0.0)

    returns_series = pd.Series(returns, index=pd.DatetimeIndex(dates), dtype=float)
    full = _metrics(returns_series, final_nav=prev_nav, initial_capital=scenario.capital)
    full.update(
        {
            "avg_holdings": round(float(np.mean(holdings_counts)), 2) if holdings_counts else 0.0,
            "max_holdings": int(max(holdings_counts)) if holdings_counts else 0,
            "avg_stock_exposure": round(float(np.mean(stock_exposures)), 4) if stock_exposures else 0.0,
            "avg_cash_weight": round(float(np.mean(cash_weights)), 4) if cash_weights else 0.0,
            "avg_target_gross": round(float(np.mean(target_gross_values)), 4)
            if target_gross_values
            else 0.0,
            "risk_off_rebalances": int(risk_off_days),
            "total_fees": round(float(total_diag["fees"]), 2),
            "fees_pct_initial_capital": round(float(total_diag["fees"] / scenario.capital), 4),
            "turnover_pct_initial_capital": round(
                float((total_diag["buy_notional"] + total_diag["sell_notional"]) / scenario.capital),
                4,
            ),
            "orders": int(total_diag["orders"]),
            "blocked_buys": int(total_diag["blocked_buys"]),
            "blocked_sells": int(total_diag["blocked_sells"]),
            "lot_blocked_targets": int(total_diag["lot_blocked_targets"]),
            "cash_blocked_buys": int(total_diag["cash_blocked_buys"]),
        }
    )
    wf = _walk_forward_from_returns(returns_series) if len(returns_series) > 2100 else {}
    minimum_gate = (
        full.get("sharpe_ratio", -999) >= 1.2
        and full.get("max_drawdown", -999) >= -0.15
        and full.get("win_rate", 0.0) >= 0.40
    )
    executable_gate = (
        full.get("avg_holdings", 0.0) >= min(3, scenario.max_positions)
        and full.get("avg_stock_exposure", 0.0) >= 0.10
        and full.get("cash_blocked_buys", 10**9) <= full.get("orders", 0) * 0.25 + 25
    )
    return {
        "name": (
            f"v33_small_account_{scenario.gross_profile}_{scenario.rank_mode}_"
            f"{int(scenario.capital)}_{scenario.max_positions}pos"
        ),
        "scenario": asdict(scenario),
        "cost": asdict(cost),
        "production_ready": False,
        "research_only": True,
        "small_account_minimum_gate_passes": bool(minimum_gate),
        "small_account_executable_gate_passes": bool(executable_gate),
        "full": full,
        "wf": wf,
        "production_blockers": [
            "small-account simulation is local research evidence only",
            "V31 trading status is free-source approximation, not vendor production evidence",
            "missing broker order/fill replay, paper trading and external production evidence gate",
        ],
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "name",
        "capital",
        "rank_mode",
        "gross_profile",
        "max_positions",
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
        "avg_stock_exposure",
        "avg_cash_weight",
        "total_fees",
        "fees_pct_initial_capital",
        "orders",
        "blocked_buys",
        "blocked_sells",
        "lot_blocked_targets",
        "cash_blocked_buys",
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
            writer.writerow(
                {
                    "name": row["name"],
                    "capital": row["scenario"]["capital"],
                    "rank_mode": row["scenario"]["rank_mode"],
                    "gross_profile": row["scenario"]["gross_profile"],
                    "max_positions": row["scenario"]["max_positions"],
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
                    "avg_stock_exposure": full.get("avg_stock_exposure"),
                    "avg_cash_weight": full.get("avg_cash_weight"),
                    "total_fees": full.get("total_fees"),
                    "fees_pct_initial_capital": full.get("fees_pct_initial_capital"),
                    "orders": full.get("orders"),
                    "blocked_buys": full.get("blocked_buys"),
                    "blocked_sells": full.get("blocked_sells"),
                    "lot_blocked_targets": full.get("lot_blocked_targets"),
                    "cash_blocked_buys": full.get("cash_blocked_buys"),
                    "avg_oos_sharpe": wf.get("avg_oos_sharpe"),
                    "min_oos_sharpe": round(float(min(oos_values)), 4) if oos_values else None,
                }
            )


def main() -> None:
    started = time.time()
    args = _parse_args()
    universe = Path(args.universe)
    output_json = Path(args.output_json)
    output_csv = Path(args.output_csv)
    status_path = Path(args.trading_status_path)
    cost = CostConfig(
        commission_rate=float(args.commission_rate),
        min_commission=float(args.min_commission),
        stamp_duty_rate=float(args.stamp_duty_rate),
        slippage_bps=float(args.slippage_bps),
    )
    capitals = _parse_float_list(args.capitals)
    max_positions = _parse_int_list(args.max_positions)
    rank_modes = [item.strip() for item in args.rank_modes.split(",") if item.strip()]
    invalid_modes = sorted(set(rank_modes) - {"ic", "lowvol_reversal", "defensive", "blend"})
    if invalid_modes:
        raise ValueError(f"unsupported rank modes: {invalid_modes}")
    profiles = [item.strip() for item in args.profiles.split(",") if item.strip()]
    invalid_profiles = sorted(set(profiles) - set(GROSS_PROFILES))
    if invalid_profiles:
        raise ValueError(f"unsupported gross profiles: {invalid_profiles}")

    codes = _read_codes(universe)
    close, volume, amount = _load_market_panel(codes, min_history=int(args.min_history))
    close = close.loc[pd.Timestamp(args.start_date) : pd.Timestamp(args.end_date)].copy()
    volume = volume.reindex(close.index)
    amount = amount.reindex(close.index)
    if len(close) < 600:
        raise RuntimeError("not enough daily data for small-account validation")
    valuation_close = close.ffill()
    factors = _build_factors(close, volume, amount)
    future_5d = close.pct_change(5, fill_method=None).shift(-5)
    rolling_ic = _rolling_ic_weights(factors, future_5d)
    regime = _build_market_regime_features(close)
    industry_map = _load_industry_map()
    trading_constraints, trading_summary = _load_trading_status_constraints(status_path, close.index, close.columns)
    if trading_constraints is None:
        raise RuntimeError(f"trading status constraints are required for V33: {status_path}")

    scenarios = [
        ScenarioConfig(
            capital=capital,
            max_positions=position_count,
            rank_mode=rank_mode,  # type: ignore[arg-type]
            gross_profile=profile,  # type: ignore[arg-type]
            max_per_industry=max(1, min(3, math.ceil(position_count / 3))),
        )
        for capital in capitals
        for position_count in max_positions
        for rank_mode in rank_modes
        for profile in profiles
    ]
    rows = [
        _run_scenario(
            scenario=scenario,
            close=close,
            valuation_close=valuation_close,
            factors=factors,
            rolling_ic=rolling_ic,
            regime=regime,
            industry_map=industry_map,
            trading_constraints=trading_constraints,
            cost=cost,
        )
        for scenario in scenarios
    ]
    rows.sort(
        key=lambda row: (
            bool(row["small_account_minimum_gate_passes"]),
            bool(row["small_account_executable_gate_passes"]),
            float(row["full"].get("sharpe_ratio", -999)),
            float(row["full"].get("annual_return", -999)),
        ),
        reverse=True,
    )
    best_by_capital: dict[str, Any] = {}
    for capital in capitals:
        capital_rows = [row for row in rows if float(row["scenario"]["capital"]) == float(capital)]
        if capital_rows:
            best_by_capital[str(int(capital))] = capital_rows[0]

    report = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V33-small-account-execution-v1",
        "research_only": True,
        "production_ready": False,
        "objective": "RMB 50k/100k executable A-share account test with 100-share lots and minimum fees",
        "universe": str(universe),
        "universe_sha256": _sha256_file(universe) if universe.exists() else None,
        "coverage": {
            "symbols_loaded": int(close.shape[1]),
            "dates": int(len(close)),
            "start": pd.Timestamp(close.index.min()).date().isoformat(),
            "end": pd.Timestamp(close.index.max()).date().isoformat(),
            "industry_overlap": int(sum(1 for symbol in close.columns if symbol in industry_map)),
        },
        "trading_constraint_summary": trading_summary,
        "cost_model": asdict(cost),
        "scenario_count": len(rows),
        "best_by_capital": best_by_capital,
        "results": rows,
        "production_blockers": [
            "research result does not approve live trading",
            "requires real broker paper trading fills and account-position reconciliation",
            "requires V30 vendor PIT/corporate-action/trading-status evidence and external production gate",
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
                "best_by_capital": {
                    capital: {
                        "name": row["name"],
                        "sharpe": row["full"].get("sharpe_ratio"),
                        "annual_return": row["full"].get("annual_return"),
                        "max_drawdown": row["full"].get("max_drawdown"),
                        "avg_holdings": row["full"].get("avg_holdings"),
                        "avg_stock_exposure": row["full"].get("avg_stock_exposure"),
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
