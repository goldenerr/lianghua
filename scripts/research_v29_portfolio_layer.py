#!/usr/bin/env python3
"""Research V29: dynamic sleeve allocation portfolio layer.

This script is research-only. It tests whether a structurally different
portfolio construction layer can improve V28's price/volume IC alpha without
relaxing production gates. It uses only lagged sleeve performance, market
regime state and precomputed factors; alt-data variants remain marked as
research-only because local archived/snapshot panels are not production PIT
entitlement evidence.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from _paths import ALT_FEATURES_DIR, HEDGE_ASSETS_DIR, PROJECT_DIR, RESULTS_DIR
from research_v28_factor_direction import (
    DEFAULT_UNIVERSE,
    PRICE_FACTOR_NAMES,
    _build_factors,
    _build_market_regime_features,
    _factor_diagnostics,
    _load_alt_factors,
    _load_crisis_returns,
    _load_industry_map,
    _load_market_panel,
    _market_regime,
    _read_codes,
    _rolling_ic_weights,
    _score_ic_adaptive,
    _score_static,
    _select_weights,
)

DEFAULT_OUTPUT_JSON = RESULTS_DIR / "quant_logic_research_v29_portfolio_layer.json"
DEFAULT_OUTPUT_CSV = RESULTS_DIR / "quant_logic_research_v29_portfolio_layer.csv"
DEFAULT_ALT_FEATURE_FILE = ALT_FEATURES_DIR / "v28_v27_archive_20260615_stock_alt_features.parquet"
DEFAULT_V31_TRADING_STATUS = (
    PROJECT_DIR / "data" / "security_master" / "free_pit_approx" / "trading_status_v31.parquet"
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument("--min-history", type=int, default=252)
    parser.add_argument("--start-date", default="20170101")
    parser.add_argument("--end-date", default="20260529")
    parser.add_argument("--alt-feature-file", default=str(DEFAULT_ALT_FEATURE_FILE))
    parser.add_argument(
        "--trading-status-path",
        default="",
        help="Optional V31 free trading-status parquet. Empty keeps legacy unconstrained research.",
    )
    parser.add_argument("--require-trading-status", action="store_true")
    return parser.parse_args()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _metrics(returns: pd.Series) -> dict[str, Any]:
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
        "total_return": round(float(equity.iloc[-1] - 1.0), 4),
        "n_days": int(len(series)),
    }


def _max_drawdown(series: pd.Series) -> float:
    if series.empty:
        return 0.0
    equity = (1.0 + series).cumprod()
    peak = equity.cummax()
    return float(((equity - peak) / peak).min())


def _load_asset_returns(file_name: str, index: pd.DatetimeIndex) -> pd.Series:
    path = HEDGE_ASSETS_DIR / file_name
    if not path.exists():
        return pd.Series(0.0, index=index, dtype=float)
    frame = pd.read_parquet(path)
    if "close" not in frame.columns:
        return pd.Series(0.0, index=index, dtype=float)
    close = pd.to_numeric(frame["close"], errors="coerce")
    close.index = pd.to_datetime(frame.index, errors="coerce").normalize()
    return close.sort_index().pct_change(fill_method=None).reindex(index).fillna(0.0)


def _pivot_status_bool(
    frame: pd.DataFrame,
    column: str,
    index: pd.DatetimeIndex,
    symbols: pd.Index,
    *,
    default: bool,
) -> pd.DataFrame:
    if column not in frame.columns:
        return pd.DataFrame(default, index=index, columns=symbols, dtype=bool)
    pivot = frame.pivot_table(index="date", columns="code", values=column, aggfunc="last")
    return pivot.reindex(index=index, columns=symbols).astype("boolean").fillna(default).astype(bool)


def _load_trading_status_constraints(
    path: Path,
    index: pd.DatetimeIndex,
    symbols: pd.Index,
) -> tuple[dict[str, pd.DataFrame] | None, dict[str, Any]]:
    if not path.exists():
        return None, {"enabled": False, "path": str(path), "exists": False}
    columns = ["date", "code", "is_tradable", "is_limit_up", "is_limit_down", "is_st_known"]
    import pyarrow.parquet as pq

    available = set(pq.ParquetFile(path).schema_arrow.names)
    read_columns = [column for column in columns if column in available]
    missing_columns = [column for column in columns if column not in available]
    if {"date", "code"} - set(read_columns):
        return None, {
            "enabled": False,
            "path": str(path),
            "exists": True,
            "missing_columns": missing_columns,
            "failure": "missing date/code columns",
        }
    frame = pd.read_parquet(path, columns=read_columns)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    frame["code"] = frame["code"].astype(str).str.extract(r"(\d{6})", expand=False).str.zfill(6)
    index_set = set(pd.DatetimeIndex(index).normalize())
    symbol_set = set(symbols.astype(str))
    frame = frame[frame["date"].isin(index_set) & frame["code"].isin(symbol_set)].dropna(
        subset=["date", "code"]
    )
    constraints = {
        "is_tradable": _pivot_status_bool(frame, "is_tradable", index, symbols, default=False),
        "is_limit_up": _pivot_status_bool(frame, "is_limit_up", index, symbols, default=False),
        "is_limit_down": _pivot_status_bool(frame, "is_limit_down", index, symbols, default=False),
    }
    return constraints, {
        "enabled": True,
        "path": str(path),
        "exists": True,
        "mode": "V31-free-trading-status-next-bar-execution",
        "rows_loaded": int(len(frame)),
        "unique_dates": int(frame["date"].nunique()) if not frame.empty else 0,
        "unique_symbols": int(frame["code"].nunique()) if not frame.empty else 0,
        "missing_columns": missing_columns,
        "limitations": [
            "V31 status is free-source research approximation, not production trading-status evidence.",
            "Orders are constrained using next bar status to stress-test tradability in daily research.",
            "ST status remains unknown in V31 and is not used as a hard constraint here.",
        ],
    }


def _apply_execution_constraints(
    *,
    old_positions: dict[str, float],
    target_positions: dict[str, float],
    is_tradable: pd.Series,
    is_limit_up: pd.Series,
    is_limit_down: pd.Series,
) -> tuple[dict[str, float], dict[str, float]]:
    constrained: dict[str, float] = {}
    blocked_buy_orders = 0
    blocked_sell_orders = 0
    blocked_buy_weight = 0.0
    blocked_sell_weight = 0.0
    eps = 1e-12
    symbols = set(old_positions) | set(target_positions)
    for symbol in symbols:
        old_weight = float(old_positions.get(symbol, 0.0))
        target_weight = float(target_positions.get(symbol, 0.0))
        delta = target_weight - old_weight
        tradable = bool(is_tradable.get(symbol, False))
        limit_up = bool(is_limit_up.get(symbol, False))
        limit_down = bool(is_limit_down.get(symbol, False))
        if delta > eps and (not tradable or limit_up):
            constrained[symbol] = old_weight
            blocked_buy_orders += 1
            blocked_buy_weight += delta
        elif delta < -eps and (not tradable or limit_down):
            constrained[symbol] = old_weight
            blocked_sell_orders += 1
            blocked_sell_weight += abs(delta)
        elif target_weight > eps:
            constrained[symbol] = target_weight

    total_weight = sum(constrained.values())
    if total_weight > 1.0 + eps:
        increases = {
            symbol: weight - float(old_positions.get(symbol, 0.0))
            for symbol, weight in constrained.items()
            if weight > float(old_positions.get(symbol, 0.0)) + eps
        }
        increase_sum = sum(increases.values())
        excess = total_weight - 1.0
        if increase_sum > eps:
            for symbol, increase in increases.items():
                constrained[symbol] = max(0.0, constrained[symbol] - excess * increase / increase_sum)
        else:
            scale = 1.0 / total_weight
            constrained = {symbol: weight * scale for symbol, weight in constrained.items()}

    constrained = {symbol: float(weight) for symbol, weight in constrained.items() if weight > eps}
    diagnostics = {
        "blocked_buy_orders": float(blocked_buy_orders),
        "blocked_sell_orders": float(blocked_sell_orders),
        "blocked_buy_weight": float(blocked_buy_weight),
        "blocked_sell_weight": float(blocked_sell_weight),
        "gross_after_constraints": float(sum(constrained.values())),
        "cash_weight_after_constraints": float(max(0.0, 1.0 - sum(constrained.values()))),
    }
    return constrained, diagnostics


def _sleeve_configs() -> dict[str, dict[str, Any]]:
    return {
        "price_ic_alpha": {
            "mode": "ic_adaptive",
            "factor_set": "price_only",
            "rebalance_freq": 10,
            "top_n": 60,
            "max_per_industry": 6,
            "cost_bps": 10,
        },
        "price_ic_diversified": {
            "mode": "ic_adaptive",
            "factor_set": "price_only",
            "rebalance_freq": 20,
            "top_n": 90,
            "max_per_industry": 9,
            "cost_bps": 8,
        },
        "price_lowvol_reversal": {
            "mode": "static",
            "factor_weights": {
                "reversal_5d": 0.20,
                "momentum_20d": -0.20,
                "momentum_60d": -0.20,
                "low_vol_20d": 0.25,
                "low_vol_60d": 0.20,
                "liquidity_20d": -0.10,
                "volume_stability_20d": 0.15,
            },
            "rebalance_freq": 10,
            "top_n": 70,
            "max_per_industry": 7,
            "cost_bps": 10,
        },
        "price_defensive_breadth": {
            "mode": "static",
            "factor_weights": {
                "low_vol_20d": 0.35,
                "low_vol_60d": 0.25,
                "reversal_5d": 0.15,
                "volume_stability_20d": 0.15,
                "liquidity_20d": -0.10,
            },
            "rebalance_freq": 20,
            "top_n": 120,
            "max_per_industry": 12,
            "cost_bps": 8,
        },
        "alt_ic_research": {
            "mode": "ic_adaptive",
            "factor_set": "all",
            "rebalance_freq": 10,
            "top_n": 70,
            "max_per_industry": 7,
            "cost_bps": 12,
            "production_comparable": False,
            "production_blocker": "uses archived/snapshot alt panels without production PIT entitlement evidence",
        },
    }


def _portfolio_configs() -> list[dict[str, Any]]:
    return [
        {
            "name": "v29_price_meta_lowvol_crisis",
            "sleeves": ["price_ic_alpha", "price_ic_diversified", "price_lowvol_reversal", "price_defensive_breadth"],
            "lookback": 126,
            "rebalance_freq": 10,
            "base_gross": 0.58,
            "bull_gross": 0.86,
            "neutral_gross": 0.58,
            "risk_off_gross": 0.18,
            "vol_target": 0.12,
            "max_sleeve_weight": 0.45,
            "temperature": 0.65,
            "dd_reduce_threshold": 0.07,
            "dd_reduce_scale": 0.45,
            "dd_stop_threshold": 0.13,
            "dd_stop_scale": 0.08,
            "meta_cost_bps": 2,
            "use_crisis_sleeve": True,
        },
        {
            "name": "v29_price_meta_balanced_crisis",
            "sleeves": ["price_ic_alpha", "price_ic_diversified", "price_lowvol_reversal", "price_defensive_breadth"],
            "lookback": 189,
            "rebalance_freq": 10,
            "base_gross": 0.64,
            "bull_gross": 0.95,
            "neutral_gross": 0.64,
            "risk_off_gross": 0.22,
            "vol_target": 0.14,
            "max_sleeve_weight": 0.50,
            "temperature": 0.75,
            "dd_reduce_threshold": 0.08,
            "dd_reduce_scale": 0.50,
            "dd_stop_threshold": 0.14,
            "dd_stop_scale": 0.10,
            "meta_cost_bps": 2,
            "use_crisis_sleeve": True,
        },
        {
            "name": "v29_price_meta_aggressive_guarded",
            "sleeves": ["price_ic_alpha", "price_ic_diversified", "price_lowvol_reversal"],
            "lookback": 126,
            "rebalance_freq": 10,
            "base_gross": 0.72,
            "bull_gross": 1.0,
            "neutral_gross": 0.72,
            "risk_off_gross": 0.28,
            "vol_target": 0.16,
            "max_sleeve_weight": 0.58,
            "temperature": 0.80,
            "dd_reduce_threshold": 0.08,
            "dd_reduce_scale": 0.50,
            "dd_stop_threshold": 0.14,
            "dd_stop_scale": 0.12,
            "meta_cost_bps": 2,
            "use_crisis_sleeve": True,
        },
        {
            "name": "v29_price_meta_ultradefensive",
            "sleeves": ["price_ic_diversified", "price_lowvol_reversal", "price_defensive_breadth"],
            "lookback": 252,
            "rebalance_freq": 20,
            "base_gross": 0.45,
            "bull_gross": 0.70,
            "neutral_gross": 0.45,
            "risk_off_gross": 0.12,
            "vol_target": 0.10,
            "max_sleeve_weight": 0.45,
            "temperature": 0.70,
            "dd_reduce_threshold": 0.06,
            "dd_reduce_scale": 0.35,
            "dd_stop_threshold": 0.10,
            "dd_stop_scale": 0.05,
            "meta_cost_bps": 1,
            "use_crisis_sleeve": True,
        },
        {
            "name": "v29_price_meta_lowvol_carry_floor",
            "sleeves": ["price_ic_alpha", "price_ic_diversified", "price_lowvol_reversal", "price_defensive_breadth"],
            "lookback": 126,
            "rebalance_freq": 10,
            "base_gross": 0.58,
            "bull_gross": 0.84,
            "neutral_gross": 0.58,
            "risk_off_gross": 0.16,
            "vol_target": 0.115,
            "max_sleeve_weight": 0.45,
            "temperature": 0.65,
            "dd_reduce_threshold": 0.06,
            "dd_reduce_scale": 0.40,
            "dd_stop_threshold": 0.10,
            "dd_stop_scale": 0.05,
            "meta_cost_bps": 2,
            "use_crisis_sleeve": True,
            "crisis_fraction": 0.25,
            "carry_fraction": 0.75,
        },
        {
            "name": "v29_price_meta_lowvol_blended_floor",
            "sleeves": ["price_ic_alpha", "price_ic_diversified", "price_lowvol_reversal", "price_defensive_breadth"],
            "lookback": 126,
            "rebalance_freq": 10,
            "base_gross": 0.58,
            "bull_gross": 0.86,
            "neutral_gross": 0.58,
            "risk_off_gross": 0.18,
            "vol_target": 0.12,
            "max_sleeve_weight": 0.45,
            "temperature": 0.65,
            "dd_reduce_threshold": 0.07,
            "dd_reduce_scale": 0.45,
            "dd_stop_threshold": 0.13,
            "dd_stop_scale": 0.08,
            "meta_cost_bps": 2,
            "use_crisis_sleeve": True,
            "crisis_fraction": 0.82,
            "carry_fraction": 0.18,
        },
        {
            "name": "v29_price_meta_lowvol_micro_carry_floor",
            "sleeves": ["price_ic_alpha", "price_ic_diversified", "price_lowvol_reversal", "price_defensive_breadth"],
            "lookback": 126,
            "rebalance_freq": 10,
            "base_gross": 0.58,
            "bull_gross": 0.86,
            "neutral_gross": 0.58,
            "risk_off_gross": 0.18,
            "vol_target": 0.12,
            "max_sleeve_weight": 0.45,
            "temperature": 0.65,
            "dd_reduce_threshold": 0.07,
            "dd_reduce_scale": 0.45,
            "dd_stop_threshold": 0.13,
            "dd_stop_scale": 0.08,
            "meta_cost_bps": 2,
            "use_crisis_sleeve": True,
            "crisis_fraction": 0.94,
            "carry_fraction": 0.06,
        },
        {
            "name": "v29_price_meta_lowvol_small_carry_floor",
            "sleeves": ["price_ic_alpha", "price_ic_diversified", "price_lowvol_reversal", "price_defensive_breadth"],
            "lookback": 126,
            "rebalance_freq": 10,
            "base_gross": 0.58,
            "bull_gross": 0.86,
            "neutral_gross": 0.58,
            "risk_off_gross": 0.18,
            "vol_target": 0.12,
            "max_sleeve_weight": 0.45,
            "temperature": 0.65,
            "dd_reduce_threshold": 0.07,
            "dd_reduce_scale": 0.45,
            "dd_stop_threshold": 0.13,
            "dd_stop_scale": 0.08,
            "meta_cost_bps": 2,
            "use_crisis_sleeve": True,
            "crisis_fraction": 0.92,
            "carry_fraction": 0.08,
        },
        {
            "name": "v29_price_meta_lowvol_recent_guard",
            "sleeves": ["price_ic_alpha", "price_ic_diversified", "price_lowvol_reversal", "price_defensive_breadth"],
            "lookback": 126,
            "rebalance_freq": 10,
            "base_gross": 0.56,
            "bull_gross": 0.82,
            "neutral_gross": 0.56,
            "risk_off_gross": 0.15,
            "vol_target": 0.11,
            "max_sleeve_weight": 0.45,
            "temperature": 0.65,
            "dd_reduce_threshold": 0.05,
            "dd_reduce_scale": 0.35,
            "dd_stop_threshold": 0.09,
            "dd_stop_scale": 0.04,
            "meta_cost_bps": 2,
            "use_crisis_sleeve": True,
            "crisis_fraction": 0.90,
            "carry_fraction": 0.10,
        },
        {
            "name": "v29_price_meta_longhorizon_guard",
            "sleeves": ["price_ic_alpha", "price_ic_diversified", "price_lowvol_reversal", "price_defensive_breadth"],
            "lookback": 126,
            "rebalance_freq": 10,
            "base_gross": 0.60,
            "bull_gross": 0.87,
            "neutral_gross": 0.60,
            "risk_off_gross": 0.16,
            "vol_target": 0.115,
            "max_sleeve_weight": 0.45,
            "temperature": 0.65,
            "dd_reduce_threshold": 0.05,
            "dd_reduce_scale": 0.35,
            "dd_stop_threshold": 0.09,
            "dd_stop_scale": 0.04,
            "meta_cost_bps": 2,
            "use_crisis_sleeve": True,
            "crisis_fraction": 0.75,
            "carry_fraction": 0.25,
        },
        {
            "name": "v29_price_meta_balanced_carry_floor",
            "sleeves": ["price_ic_alpha", "price_ic_diversified", "price_lowvol_reversal", "price_defensive_breadth"],
            "lookback": 189,
            "rebalance_freq": 10,
            "base_gross": 0.62,
            "bull_gross": 0.90,
            "neutral_gross": 0.62,
            "risk_off_gross": 0.18,
            "vol_target": 0.13,
            "max_sleeve_weight": 0.48,
            "temperature": 0.72,
            "dd_reduce_threshold": 0.07,
            "dd_reduce_scale": 0.45,
            "dd_stop_threshold": 0.11,
            "dd_stop_scale": 0.07,
            "meta_cost_bps": 2,
            "use_crisis_sleeve": True,
            "crisis_fraction": 0.30,
            "carry_fraction": 0.70,
        },
        {
            "name": "v29_price_meta_balanced_blended_floor",
            "sleeves": ["price_ic_alpha", "price_ic_diversified", "price_lowvol_reversal", "price_defensive_breadth"],
            "lookback": 189,
            "rebalance_freq": 10,
            "base_gross": 0.64,
            "bull_gross": 0.95,
            "neutral_gross": 0.64,
            "risk_off_gross": 0.22,
            "vol_target": 0.14,
            "max_sleeve_weight": 0.50,
            "temperature": 0.75,
            "dd_reduce_threshold": 0.08,
            "dd_reduce_scale": 0.50,
            "dd_stop_threshold": 0.14,
            "dd_stop_scale": 0.10,
            "meta_cost_bps": 2,
            "use_crisis_sleeve": True,
            "crisis_fraction": 0.80,
            "carry_fraction": 0.20,
        },
        {
            "name": "v29_alt_meta_research_only",
            "sleeves": ["price_ic_alpha", "price_lowvol_reversal", "price_defensive_breadth", "alt_ic_research"],
            "lookback": 126,
            "rebalance_freq": 10,
            "base_gross": 0.62,
            "bull_gross": 0.90,
            "neutral_gross": 0.62,
            "risk_off_gross": 0.20,
            "vol_target": 0.13,
            "max_sleeve_weight": 0.50,
            "temperature": 0.70,
            "dd_reduce_threshold": 0.07,
            "dd_reduce_scale": 0.45,
            "dd_stop_threshold": 0.13,
            "dd_stop_scale": 0.08,
            "meta_cost_bps": 2,
            "use_crisis_sleeve": True,
            "production_comparable": False,
            "production_blocker": "includes archived/snapshot alt sleeve without production PIT entitlement evidence",
        },
    ]


def _run_stock_sleeve(
    name: str,
    config: dict[str, Any],
    close: pd.DataFrame,
    daily_stock_returns: pd.DataFrame,
    factors: dict[str, pd.DataFrame],
    rolling_ic: pd.DataFrame,
    industry_map: dict[str, str],
    trading_constraints: dict[str, pd.DataFrame] | None = None,
) -> tuple[pd.Series, dict[str, Any]]:
    positions: dict[str, float] = {}
    returns: list[float] = []
    dates: list[pd.Timestamp] = []
    turnovers: list[float] = []
    active_counts: list[int] = []
    gross_weights: list[float] = []
    cash_weights: list[float] = []
    blocked_buy_orders = 0.0
    blocked_sell_orders = 0.0
    blocked_buy_weight = 0.0
    blocked_sell_weight = 0.0
    constrained_rebalances = 0
    cost_rate = float(config.get("cost_bps", 10)) / 10000.0
    start_idx = 252
    for t in range(start_idx, len(close) - 1):
        if (t - start_idx) % int(config["rebalance_freq"]) == 0:
            if config["mode"] == "ic_adaptive":
                factor_names = None if config.get("factor_set") == "all" else PRICE_FACTOR_NAMES
                score = _score_ic_adaptive(factors, rolling_ic, t, factor_names=factor_names)
            else:
                score = _score_static(factors, t, config["factor_weights"])
            new_positions = _select_weights(
                score,
                industry_map,
                top_n=int(config["top_n"]),
                max_per_industry=int(config["max_per_industry"]),
                gross=1.0,
            )
            if trading_constraints is not None:
                trade_date = pd.Timestamp(close.index[t + 1])
                new_positions, constraint_diag = _apply_execution_constraints(
                    old_positions=positions,
                    target_positions=new_positions,
                    is_tradable=trading_constraints["is_tradable"].loc[trade_date],
                    is_limit_up=trading_constraints["is_limit_up"].loc[trade_date],
                    is_limit_down=trading_constraints["is_limit_down"].loc[trade_date],
                )
                blocked_buy_orders += constraint_diag["blocked_buy_orders"]
                blocked_sell_orders += constraint_diag["blocked_sell_orders"]
                blocked_buy_weight += constraint_diag["blocked_buy_weight"]
                blocked_sell_weight += constraint_diag["blocked_sell_weight"]
                constrained_rebalances += int(
                    constraint_diag["blocked_buy_orders"] > 0
                    or constraint_diag["blocked_sell_orders"] > 0
                    or constraint_diag["cash_weight_after_constraints"] > 1e-9
                )
            symbols = set(positions) | set(new_positions)
            turnover = sum(abs(new_positions.get(symbol, 0.0) - positions.get(symbol, 0.0)) for symbol in symbols)
            positions = new_positions
        else:
            turnover = 0.0
        next_returns = daily_stock_returns.iloc[t + 1]
        gross_return = sum(weight * float(next_returns.get(symbol, 0.0)) for symbol, weight in positions.items())
        returns.append(gross_return - turnover * cost_rate)
        dates.append(pd.Timestamp(close.index[t + 1]))
        turnovers.append(turnover)
        active_counts.append(len(positions))
        gross = float(sum(abs(weight) for weight in positions.values()))
        gross_weights.append(gross)
        cash_weights.append(max(0.0, 1.0 - gross))
    series = pd.Series(returns, index=pd.DatetimeIndex(dates), name=name, dtype=float)
    diagnostics = {
        "name": name,
        "config": config,
        "metrics": _metrics(series),
        "avg_turnover": round(float(np.mean(turnovers)), 4) if turnovers else 0.0,
        "avg_active_stocks": round(float(np.mean(active_counts)), 2) if active_counts else 0.0,
        "max_active_stocks": int(max(active_counts)) if active_counts else 0,
        "execution_constraints_enabled": trading_constraints is not None,
        "constrained_rebalances": int(constrained_rebalances),
        "blocked_buy_orders": int(blocked_buy_orders),
        "blocked_sell_orders": int(blocked_sell_orders),
        "blocked_buy_weight": round(float(blocked_buy_weight), 6),
        "blocked_sell_weight": round(float(blocked_sell_weight), 6),
        "avg_gross_after_constraints": round(float(np.mean(gross_weights)), 4) if gross_weights else 0.0,
        "avg_cash_after_constraints": round(float(np.mean(cash_weights)), 4) if cash_weights else 0.0,
    }
    return series, diagnostics


def _cap_weights(raw: pd.Series, max_weight: float) -> pd.Series:
    weights = raw.clip(lower=0.0).astype(float)
    if weights.sum() <= 1e-12:
        return pd.Series(1.0 / len(raw), index=raw.index, dtype=float)
    weights = weights / weights.sum()
    for _ in range(10):
        over = weights > max_weight
        if not bool(over.any()):
            break
        capped_total = float(weights[over].sum())
        weights[over] = max_weight
        residual = 1.0 - float(weights[over].sum())
        under = ~over
        under_sum = float(weights[under].sum())
        if residual <= 0 or under_sum <= 1e-12:
            break
        weights[under] = weights[under] / under_sum * residual
        if abs(capped_total - float(weights[over].sum())) < 1e-12:
            break
    return weights / weights.sum()


def _allocate_from_history(history: pd.DataFrame, config: dict[str, Any]) -> pd.Series:
    if len(history) < 40:
        return pd.Series(1.0 / history.shape[1], index=history.columns, dtype=float)
    ann_ret = history.mean() * 252
    ann_vol = history.std(ddof=1) * np.sqrt(252)
    sharpe = (ann_ret - 0.025) / ann_vol.replace(0.0, np.nan)
    recent = history.tail(21).mean() * 252
    drawdown = pd.Series({_col: _max_drawdown(history[_col]) for _col in history.columns})
    corr = history.corr().replace([np.inf, -np.inf], np.nan).fillna(0.0)
    avg_corr = corr.mean().reindex(history.columns).fillna(0.0)
    score = sharpe.fillna(-2.0) + 0.20 * recent.fillna(0.0) + 1.50 * drawdown.fillna(-0.5) - 0.25 * avg_corr
    score = score.clip(-3.0, 3.0)
    temperature = max(float(config.get("temperature", 0.75)), 0.1)
    raw = np.exp((score - score.max()) / temperature)
    return _cap_weights(pd.Series(raw, index=history.columns, dtype=float), float(config["max_sleeve_weight"]))


def _run_meta_portfolio(
    config: dict[str, Any],
    sleeve_returns: pd.DataFrame,
    crisis_returns: pd.Series,
    carry_returns: pd.Series,
    close_index: pd.DatetimeIndex,
    regime: dict[str, np.ndarray],
) -> dict[str, Any]:
    selected = sleeve_returns[list(config["sleeves"])].dropna(how="any")
    crisis = crisis_returns.reindex(selected.index).fillna(0.0)
    carry = carry_returns.reindex(selected.index).fillna(0.0)
    returns: list[float] = []
    dates: list[pd.Timestamp] = []
    gross_exposures: list[float] = []
    crisis_weights: list[float] = []
    carry_weights: list[float] = []
    sleeve_weight_rows: list[dict[str, float]] = []
    risk_off_days = 0
    old_scaled = pd.Series(0.0, index=selected.columns, dtype=float)
    old_crisis_weight = 0.0
    old_carry_weight = 0.0
    alloc = pd.Series(1.0 / len(selected.columns), index=selected.columns, dtype=float)
    equity = 1.0
    peak_equity = 1.0
    cost_rate = float(config.get("meta_cost_bps", 2)) / 10000.0
    start = max(int(config.get("lookback", 126)), 60)
    for i in range(start, len(selected)):
        date = pd.Timestamp(selected.index[i])
        if (i - start) % int(config["rebalance_freq"]) == 0:
            history = selected.iloc[max(0, i - int(config["lookback"])) : i]
            alloc = _allocate_from_history(history, config)
        close_loc = int(close_index.get_loc(date)) if date in close_index else min(i + 252, len(close_index) - 1)
        gross, risk_off, market_vol = _market_regime(regime, close_loc, config)
        gross = min(float(config.get("base_gross", gross)), gross) if risk_off else gross
        if market_vol > 1e-8 and config.get("vol_target") is not None:
            gross *= min(1.0, float(config["vol_target"]) / market_vol)
        current_dd = (equity - peak_equity) / peak_equity if peak_equity > 0 else 0.0
        if current_dd < -float(config.get("dd_stop_threshold", 1.0)):
            gross *= float(config.get("dd_stop_scale", 0.0))
        elif current_dd < -float(config.get("dd_reduce_threshold", 1.0)):
            gross *= float(config.get("dd_reduce_scale", 0.5))
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
        stock_return = float((selected.iloc[i] * scaled).sum())
        net_return = (
            stock_return
            + crisis_weight * float(crisis.iloc[i])
            + carry_weight * float(carry.iloc[i])
            - turnover * cost_rate
        )
        equity *= 1.0 + net_return
        peak_equity = max(peak_equity, equity)
        returns.append(net_return)
        dates.append(date)
        gross_exposures.append(float(scaled.abs().sum()))
        crisis_weights.append(float(crisis_weight))
        carry_weights.append(float(carry_weight))
        sleeve_weight_rows.append({name: round(float(value), 6) for name, value in scaled.items()})
        risk_off_days += int(risk_off)
        old_scaled = scaled
        old_crisis_weight = crisis_weight
        old_carry_weight = carry_weight
    portfolio_returns = pd.Series(returns, index=pd.DatetimeIndex(dates), dtype=float)
    full = _metrics(portfolio_returns)
    full.update(
        {
            "avg_stock_exposure": round(float(np.mean(gross_exposures)), 4) if gross_exposures else 0.0,
            "avg_crisis_weight": round(float(np.mean(crisis_weights)), 4) if crisis_weights else 0.0,
            "avg_carry_weight": round(float(np.mean(carry_weights)), 4) if carry_weights else 0.0,
            "avg_active_stocks": None,
            "risk_off_rebalances": risk_off_days,
        }
    )
    return {
        "returns": portfolio_returns,
        "full": full,
        "allocation_tail": sleeve_weight_rows[-5:],
    }


def _walk_forward_from_returns(returns: pd.Series) -> dict[str, Any]:
    """Return fixed-candidate chronological diagnostics, not a valid WF fit."""
    test_days = 252
    warmup = 756
    folds: list[dict[str, Any]] = []
    latest_end = len(returns) - 1
    fold_starts = [latest_end - test_days * idx for idx in range(5, 0, -1)]
    for fold, test_start in enumerate(fold_starts):
        test_end = min(test_start + test_days, latest_end)
        context_start = max(0, test_start - warmup)
        is_result = _metrics(returns.iloc[context_start:test_start])
        oos_result = _metrics(returns.iloc[test_start:test_end])
        folds.append(
            {
                "fold": fold,
                "test_start": returns.index[test_start].date().isoformat(),
                "test_end": returns.index[test_end].date().isoformat(),
                "is": is_result.get("sharpe_ratio"),
                "oos": oos_result.get("sharpe_ratio"),
                "mdd": oos_result.get("max_drawdown"),
            }
        )
    is_values = [float(item["is"]) for item in folds if item.get("is") is not None]
    oos_values = [float(item["oos"]) for item in folds if item.get("oos") is not None]
    avg_is = float(np.mean(is_values)) if is_values else 0.0
    avg_oos = float(np.mean(oos_values)) if oos_values else 0.0
    decay = (avg_is - avg_oos) / abs(avg_is) if abs(avg_is) > 1e-12 else 0.0
    return {
        "valid": False,
        "validation_type": "fixed_candidate_chronological_diagnostics",
        "invalid_reason": "no fold-internal candidate selection; use top-level walk_forward_selection",
        "folds": folds,
        "avg_is_sharpe": round(avg_is, 4),
        "avg_oos_sharpe": round(avg_oos, 4),
        "sharpe_decay": round(decay, 4),
    }


def _walk_forward_select_candidates(
    candidate_returns: dict[str, pd.Series],
    *,
    production_comparable: dict[str, bool],
    n_folds: int = 5,
    train_days: int = 756,
    test_days: int = 252,
) -> dict[str, Any]:
    """Select on each train fold, freeze the candidate, then evaluate OOS."""
    eligible = sorted(
        name for name in candidate_returns if bool(production_comparable.get(name, False))
    )
    if not eligible:
        return {
            "valid": False,
            "selection_mode": "train_only_candidate_selection",
            "oos_data_used_for_selection": False,
            "invalid_reason": "no production-comparable candidates",
            "folds": [],
        }
    frame = pd.concat(
        {name: pd.to_numeric(candidate_returns[name], errors="coerce") for name in eligible},
        axis=1,
    ).replace([np.inf, -np.inf], np.nan)
    frame = frame.sort_index().dropna(how="any")
    train_n = int(train_days)
    test_n = int(test_days)
    requested_folds = int(n_folds)
    available_folds = max(0, (len(frame) - train_n) // test_n)
    if available_folds < requested_folds:
        return {
            "valid": False,
            "selection_mode": "train_only_candidate_selection",
            "oos_data_used_for_selection": False,
            "invalid_reason": "insufficient aligned history for all requested folds",
            "folds": [],
        }
    max_folds = requested_folds

    first_test_start = len(frame) - max_folds * test_n
    folds: list[dict[str, Any]] = []
    for fold in range(max_folds):
        test_start = first_test_start + fold * test_n
        test_end = test_start + test_n
        train_start = test_start - train_n
        train = frame.iloc[train_start:test_start]
        test = frame.iloc[test_start:test_end]
        train_metrics = {name: _metrics(train[name]) for name in eligible}
        selected = max(
            eligible,
            key=lambda name: float(train_metrics[name].get("sharpe_ratio", -999.0)),
        )
        selected_train = train_metrics[selected]
        selected_oos = _metrics(test[selected])
        folds.append(
            {
                "fold": fold,
                "train_start": train.index[0].date().isoformat(),
                "train_end": train.index[-1].date().isoformat(),
                "test_start": test.index[0].date().isoformat(),
                "test_end": test.index[-1].date().isoformat(),
                "selected_candidate": selected,
                "selection_frozen_for_oos": True,
                "train_candidate_metrics": train_metrics,
                "is": selected_train.get("sharpe_ratio"),
                "oos": selected_oos.get("sharpe_ratio"),
                "mdd": selected_oos.get("max_drawdown"),
                "test_n_days": int(len(test)),
            }
        )

    is_values = [float(item["is"]) for item in folds if item.get("is") is not None]
    oos_values = [float(item["oos"]) for item in folds if item.get("oos") is not None]
    avg_is = float(np.mean(is_values)) if is_values else 0.0
    avg_oos = float(np.mean(oos_values)) if oos_values else 0.0
    decay = (avg_is - avg_oos) / abs(avg_is) if abs(avg_is) > 1e-12 else 0.0
    return {
        "valid": True,
        "selection_mode": "train_only_candidate_selection",
        "training_operation": "select one fixed causal candidate by train Sharpe",
        "oos_data_used_for_selection": False,
        "production_candidates": eligible,
        "n_folds": len(folds),
        "train_days": train_n,
        "test_days": test_n,
        "folds": folds,
        "avg_is_sharpe": round(avg_is, 4),
        "avg_oos_sharpe": round(avg_oos, 4),
        "sharpe_decay": round(decay, 4),
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "name",
        "production_comparable",
        "sharpe_ratio",
        "annual_return",
        "annual_volatility",
        "max_drawdown",
        "win_rate",
        "total_return",
        "avg_oos_sharpe",
        "sharpe_decay",
        "avg_stock_exposure",
        "avg_crisis_weight",
        "avg_carry_weight",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            full = row["full"]
            writer.writerow(
                {
                    "name": row["name"],
                    "production_comparable": row.get("production_comparable"),
                    "sharpe_ratio": full.get("sharpe_ratio"),
                    "annual_return": full.get("annual_return"),
                    "annual_volatility": full.get("annual_volatility"),
                    "max_drawdown": full.get("max_drawdown"),
                    "win_rate": full.get("win_rate"),
                    "total_return": full.get("total_return"),
                    "avg_oos_sharpe": row["wf"].get("avg_oos_sharpe"),
                    "sharpe_decay": row["wf"].get("sharpe_decay"),
                    "avg_stock_exposure": full.get("avg_stock_exposure"),
                    "avg_crisis_weight": full.get("avg_crisis_weight"),
                    "avg_carry_weight": full.get("avg_carry_weight"),
                }
            )


def main() -> None:
    args = _parse_args()
    started = time.perf_counter()
    universe_path = Path(args.universe)
    codes = _read_codes(universe_path)
    close, volume, amount = _load_market_panel(codes, min_history=args.min_history)
    start = pd.Timestamp(datetime.strptime(args.start_date, "%Y%m%d")).normalize()
    end = pd.Timestamp(datetime.strptime(args.end_date, "%Y%m%d")).normalize()
    close = close.loc[(close.index >= start) & (close.index <= end)].copy()
    volume = volume.reindex(close.index)
    amount = amount.reindex(close.index)
    trading_status_path = Path(args.trading_status_path) if str(args.trading_status_path).strip() else None
    trading_constraints: dict[str, pd.DataFrame] | None = None
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
        if args.require_trading_status and trading_constraints is None:
            raise RuntimeError(f"required trading status constraints are unavailable: {trading_constraint_summary}")
    elif args.require_trading_status:
        raise RuntimeError("--require-trading-status needs --trading-status-path")
    daily_stock_returns = close.pct_change(fill_method=None).fillna(0.0)
    regime = _build_market_regime_features(close)
    factors = _build_factors(close, volume, amount)
    alt_factors, alt_summary = _load_alt_factors(Path(args.alt_feature_file), close.index, close.columns)
    factors.update(alt_factors)
    future_5d = close.shift(-5) / close - 1.0
    diagnostics = _factor_diagnostics(factors, future_5d)
    rolling_ic = _rolling_ic_weights(factors, future_5d, label_horizon=5)
    industry_map = _load_industry_map()
    crisis_returns = _load_crisis_returns(close.index)
    carry_returns = _load_asset_returns("etf_511260.parquet", close.index)

    sleeve_series: dict[str, pd.Series] = {}
    sleeve_diagnostics: dict[str, Any] = {}
    for name, config in _sleeve_configs().items():
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

    version = (
        "V29-dynamic-sleeve-portfolio-layer-v31-execution-constrained"
        if trading_constraints is not None
        else "V29-dynamic-sleeve-portfolio-layer"
    )
    results: list[dict[str, Any]] = []
    candidate_returns: dict[str, pd.Series] = {}
    production_comparability: dict[str, bool] = {}
    for config in _portfolio_configs():
        run = _run_meta_portfolio(config, sleeve_frame, crisis_returns, carry_returns, close.index, regime)
        wf = _walk_forward_from_returns(run["returns"])
        production_comparable = bool(config.get("production_comparable", True))
        candidate_returns[config["name"]] = run["returns"]
        production_comparability[config["name"]] = production_comparable
        blockers = [config["production_blocker"]] if config.get("production_blocker") else []
        results.append(
            {
                "name": config["name"],
                "version": version,
                "production_comparable": production_comparable,
                "production_blockers": blockers,
                "config": config,
                "full": run["full"],
                "wf": wf,
                "allocation_tail": run["allocation_tail"],
                "gates": {
                    "S": run["full"].get("sharpe_ratio", -999) >= 1.2,
                    "M": run["full"].get("max_drawdown", -999) >= -0.15,
                    "W": False,
                    "D": False,
                },
            }
        )
    results.sort(
        key=lambda item: (
            bool(item.get("production_comparable")),
            float(item["full"].get("sharpe_ratio", -999)),
            float(item["wf"].get("avg_oos_sharpe", -999)),
        ),
        reverse=True,
    )
    walk_forward_selection = _walk_forward_select_candidates(
        candidate_returns,
        production_comparable=production_comparability,
    )
    walk_forward_gates = {
        "W": bool(walk_forward_selection.get("valid"))
        and float(walk_forward_selection.get("avg_oos_sharpe", -999.0)) >= 0.84,
        "D": bool(walk_forward_selection.get("valid"))
        and float(walk_forward_selection.get("sharpe_decay", 999.0)) <= 0.30,
    }

    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": version,
        "research_only": True,
        "production_ready": False,
        "universe": str(universe_path),
        "universe_sha256": _sha256_file(universe_path) if universe_path.exists() else None,
        "coverage": {
            "symbols_loaded": int(close.shape[1]),
            "dates": int(close.shape[0]),
            "industry_overlap": int(len(set(close.columns) & set(industry_map))),
        },
        "alt_feature_summary": alt_summary,
        "trading_constraint_summary": trading_constraint_summary,
        "factor_diagnostics": diagnostics,
        "sleeve_diagnostics": sleeve_diagnostics,
        "residual_sleeves": {
            "crisis": _metrics(crisis_returns),
            "carry": _metrics(carry_returns),
            "carry_source": str(HEDGE_ASSETS_DIR / "etf_511260.parquet"),
        },
        "walk_forward_selection": walk_forward_selection,
        "walk_forward_gates": walk_forward_gates,
        "results": results,
        "production_blockers": [
            "research script only; not an approved strategy artifact",
            "external WORM/provider entitlement/secret/approval/broker evidence remains required",
            "alt-data variants require production PIT entitlement evidence before comparison",
            "candidate-level chronological diagnostics are not valid walk-forward gates",
            "must run approved paper-trading service and capital-impact approval before promotion",
        ],
        "elapsed_seconds": round(time.perf_counter() - started, 2),
    }
    output_json = Path(args.output_json)
    output_csv = Path(args.output_csv)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(output_csv, results)
    best = results[0] if results else {}
    print(f"Wrote {output_json}")
    print(f"Wrote {output_csv}")
    if best:
        print(
            "Best V29: "
            f"{best['name']} full_sharpe={best['full'].get('sharpe_ratio')} "
            f"mdd={best['full'].get('max_drawdown')} "
            f"wf_selection_oos={walk_forward_selection.get('avg_oos_sharpe')} "
            f"wf_selection_decay={walk_forward_selection.get('sharpe_decay')}"
        )


if __name__ == "__main__":
    main()
