#!/usr/bin/env python3
"""Research V28: factor-direction diagnostics and regime-aware sleeves.

This script is research-only. It tests whether the V27 universe needs a new
alpha stack rather than more V5.9 parameter tweaks by combining only lagged
price/volume factors, industry demeaning, rolling IC direction and a simple
crisis-asset sleeve.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from _paths import ALT_FEATURES_DIR, DATA_DIR, HEDGE_ASSETS_DIR, PROJECT_DIR, RESULTS_DIR

DEFAULT_UNIVERSE = PROJECT_DIR / "data" / "stock_list_provider_qualified_non_largecap_2000_v27.json"
DEFAULT_OUTPUT_JSON = RESULTS_DIR / "quant_logic_research_v28_factor_direction.json"
DEFAULT_OUTPUT_CSV = RESULTS_DIR / "quant_logic_research_v28_factor_direction.csv"
DEFAULT_ALT_FEATURE_FILE = ALT_FEATURES_DIR / "v28_v27_archive_20260615_stock_alt_features.parquet"
PRICE_FACTOR_NAMES = {
    "reversal_1d",
    "reversal_5d",
    "momentum_20d",
    "momentum_60d",
    "low_vol_20d",
    "low_vol_60d",
    "liquidity_20d",
    "volume_stability_20d",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument("--min-history", type=int, default=252)
    parser.add_argument("--start-date", default="20170101")
    parser.add_argument("--end-date", default="20260529")
    parser.add_argument("--alt-feature-file", default=str(DEFAULT_ALT_FEATURE_FILE))
    return parser.parse_args()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_codes(path: Path) -> list[str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise RuntimeError(f"universe must be a JSON string array: {path}")
    return [str(item).zfill(6) for item in raw]


def _load_market_panel(codes: list[str], *, min_history: int) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    close: dict[str, pd.Series] = {}
    volume: dict[str, pd.Series] = {}
    amount: dict[str, pd.Series] = {}
    for code in codes:
        path = DATA_DIR / f"{code}.parquet"
        if not path.exists():
            continue
        frame = pd.read_parquet(path)
        if len(frame) < min_history or not {"close", "volume", "amount"}.issubset(frame.columns):
            continue
        index = pd.to_datetime(frame.index, errors="coerce")
        frame = frame.loc[index.notna()].copy()
        frame.index = pd.DatetimeIndex(index[index.notna()]).normalize()
        close[code] = pd.to_numeric(frame["close"], errors="coerce")
        volume[code] = pd.to_numeric(frame["volume"], errors="coerce")
        amount[code] = pd.to_numeric(frame["amount"], errors="coerce")
    if not close:
        raise RuntimeError("no V27 daily panel available")
    close_df = pd.DataFrame(close).sort_index()
    volume_df = pd.DataFrame(volume).reindex(close_df.index)
    amount_df = pd.DataFrame(amount).reindex(close_df.index)
    return close_df, volume_df, amount_df


def _load_industry_map() -> dict[str, str]:
    path = PROJECT_DIR / "data" / "industry_fixed.parquet"
    if not path.exists():
        return {}
    frame = pd.read_parquet(path)
    if "code" not in frame.columns or "industry" not in frame.columns:
        return {}
    out: dict[str, str] = {}
    for row in frame[["code", "industry"]].itertuples(index=False):
        code = str(row.code).zfill(6)
        industry = str(row.industry).strip()
        if code and industry and industry.lower() != "nan":
            out[code] = industry
    return out


def _load_crisis_returns(index: pd.DatetimeIndex) -> pd.Series:
    preferred = ["etf_511880.parquet", "etf_518880.parquet", "etf_513100.parquet"]
    returns: list[pd.Series] = []
    for file_name in preferred:
        path = HEDGE_ASSETS_DIR / file_name
        if not path.exists():
            continue
        frame = pd.read_parquet(path)
        if "close" not in frame.columns:
            continue
        close = pd.to_numeric(frame["close"], errors="coerce")
        close.index = pd.to_datetime(frame.index, errors="coerce").normalize()
        returns.append(close.sort_index().pct_change(fill_method=None).reindex(index).fillna(0.0))
    if not returns:
        return pd.Series(0.0, index=index)
    return pd.concat(returns, axis=1).mean(axis=1).fillna(0.0)


def _zscore(series: pd.Series) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)
    mask = np.isfinite(values)
    if int(mask.sum()) < 20:
        return pd.Series(dtype=float)
    valid_values = values[mask]
    std = float(valid_values.std(ddof=1))
    if not math.isfinite(std) or std <= 1e-12:
        return pd.Series(dtype=float)
    z = np.clip((valid_values - float(valid_values.mean())) / std, -4.0, 4.0)
    return pd.Series(z, index=series.index[mask], dtype=float)


def _zscore_array(series: pd.Series) -> np.ndarray | None:
    values = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)
    mask = np.isfinite(values)
    if int(mask.sum()) < 20:
        return None
    valid_values = values[mask]
    std = float(valid_values.std(ddof=1))
    if not math.isfinite(std) or std <= 1e-12:
        return None
    out = np.full(len(values), np.nan, dtype=float)
    out[mask] = np.clip((valid_values - float(valid_values.mean())) / std, -4.0, 4.0)
    return out


def _build_factors(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    amount: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    returns_1 = close.pct_change(fill_method=None)
    volume_mean_20 = volume.rolling(20, min_periods=10).mean()
    amount_safe = amount.replace(0.0, np.nan)
    factors = {
        "reversal_1d": -returns_1,
        "reversal_5d": -close.pct_change(5, fill_method=None),
        "momentum_20d": close.pct_change(20, fill_method=None),
        "momentum_60d": close.pct_change(60, fill_method=None),
        "low_vol_20d": -returns_1.rolling(20, min_periods=15).std(ddof=1),
        "low_vol_60d": -returns_1.rolling(60, min_periods=40).std(ddof=1),
        "liquidity_20d": -(returns_1.abs() / amount_safe).rolling(20, min_periods=10).mean(),
        "volume_stability_20d": -(volume / volume_mean_20 - 1.0).abs(),
    }
    return {name: frame.replace([np.inf, -np.inf], np.nan) for name, frame in factors.items()}


def _pivot_alt_feature(
    frame: pd.DataFrame,
    column: str,
    index: pd.DatetimeIndex,
    symbols: pd.Index,
    *,
    fill_missing_zero: bool = False,
) -> pd.DataFrame:
    if column not in frame.columns:
        return pd.DataFrame(index=index, columns=symbols, dtype=float)
    pivot = frame.pivot_table(index="date", columns="code", values=column, aggfunc="last")
    out = pivot.reindex(index=index, columns=symbols)
    out = out.apply(pd.to_numeric, errors="coerce")
    if fill_missing_zero:
        out = out.fillna(0.0)
    return out.replace([np.inf, -np.inf], np.nan)


def _load_alt_factors(path: Path, index: pd.DatetimeIndex, symbols: pd.Index) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    if not path.exists():
        return {}, {"path": str(path), "exists": False}
    columns = [
        "date",
        "code",
        "analyst_revision_score_sum",
        "analyst_upgrade_minus_downgrade",
        "analyst_rating_score_mean",
        "northbound_hold_pct_change_5d",
        "northbound_net_buy_amount",
        "margin_financing_balance_change_5d",
        "margin_short_sell_volume",
        "intraday_reversal_alpha",
        "amihud_5m",
        "close_vs_vwap",
        "fund_flow_net_5d",
        "big_deal_buy_sell_imbalance",
    ]
    import pyarrow.parquet as pq

    parquet_columns = set(pq.ParquetFile(path).schema_arrow.names)
    available_columns = [column for column in columns if column in parquet_columns]
    frame = pd.read_parquet(path, columns=available_columns)
    frame["code"] = frame["code"].astype(str).str.zfill(6)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    frame = frame[frame["code"].isin(set(symbols)) & frame["date"].isin(set(index))]
    frame = frame.dropna(subset=["code", "date"]).sort_values(["date", "code"])
    factors = {
        "alt_analyst_revision_sum": _pivot_alt_feature(
            frame, "analyst_revision_score_sum", index, symbols, fill_missing_zero=True
        ),
        "alt_analyst_upgrade_minus_downgrade": _pivot_alt_feature(
            frame, "analyst_upgrade_minus_downgrade", index, symbols, fill_missing_zero=True
        ),
        "alt_analyst_rating_score": _pivot_alt_feature(
            frame, "analyst_rating_score_mean", index, symbols
        ),
        "alt_northbound_hold_change": _pivot_alt_feature(
            frame, "northbound_hold_pct_change_5d", index, symbols
        ),
        "alt_northbound_net_buy": _pivot_alt_feature(
            frame, "northbound_net_buy_amount", index, symbols, fill_missing_zero=True
        ),
        "alt_margin_financing_change": _pivot_alt_feature(
            frame, "margin_financing_balance_change_5d", index, symbols, fill_missing_zero=True
        ),
        "alt_margin_short_pressure": -_pivot_alt_feature(
            frame, "margin_short_sell_volume", index, symbols, fill_missing_zero=True
        ),
        "alt_intraday_reversal": _pivot_alt_feature(frame, "intraday_reversal_alpha", index, symbols),
        "alt_intraday_liquidity": -_pivot_alt_feature(frame, "amihud_5m", index, symbols),
        "alt_close_vs_vwap_reversal": -_pivot_alt_feature(frame, "close_vs_vwap", index, symbols),
        "alt_fund_flow_net_5d": _pivot_alt_feature(
            frame, "fund_flow_net_5d", index, symbols, fill_missing_zero=True
        ),
        "alt_big_deal_imbalance": _pivot_alt_feature(
            frame, "big_deal_buy_sell_imbalance", index, symbols, fill_missing_zero=True
        ),
    }
    non_empty = {
        name: factor
        for name, factor in factors.items()
        if int(factor.notna().sum().sum()) > 0
    }
    coverage = {
        name: {
            "non_null_cells": int(factor.notna().sum().sum()),
            "non_null_dates": int((factor.notna().sum(axis=1) > 0).sum()),
            "max_symbols_per_date": int(factor.notna().sum(axis=1).max()) if len(factor) else 0,
        }
        for name, factor in non_empty.items()
    }
    return non_empty, {
        "path": str(path),
        "exists": True,
        "rows": int(len(frame)),
        "symbols": int(frame["code"].nunique()),
        "dates": int(frame["date"].nunique()),
        "factor_coverage": coverage,
    }


def _cross_sectional_ic(factor: pd.DataFrame, future_return: pd.DataFrame) -> pd.Series:
    values: list[float] = []
    dates: list[pd.Timestamp] = []
    for date in factor.index.intersection(future_return.index):
        lhs = factor.loc[date]
        rhs = future_return.loc[date]
        joined = pd.concat([lhs, rhs], axis=1, keys=["factor", "future"]).dropna()
        dates.append(pd.Timestamp(date))
        if len(joined) < 80:
            values.append(np.nan)
        else:
            factor_rank = joined["factor"].rank()
            future_rank = joined["future"].rank()
            if factor_rank.std(ddof=1) <= 1e-12 or future_rank.std(ddof=1) <= 1e-12:
                values.append(np.nan)
            else:
                values.append(float(factor_rank.corr(future_rank)))
    return pd.Series(values, index=pd.DatetimeIndex(dates), dtype=float)


def _factor_diagnostics(factors: dict[str, pd.DataFrame], future_return: pd.DataFrame) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {}
    for name, factor in factors.items():
        ic = _cross_sectional_ic(factor, future_return)
        recent = ic.loc[ic.index >= pd.Timestamp("2024-01-01")]
        diagnostics[name] = {
            "mean_ic": round(float(ic.mean(skipna=True)), 6) if ic.notna().any() else None,
            "recent_mean_ic": round(float(recent.mean(skipna=True)), 6) if recent.notna().any() else None,
            "ic_ir": round(float(ic.mean(skipna=True) / ic.std(skipna=True)), 6)
            if ic.notna().sum() > 20 and ic.std(skipna=True) > 1e-12
            else None,
            "positive_ic_ratio": round(float((ic > 0).mean()), 6) if len(ic) else None,
            "observations": int(ic.notna().sum()),
        }
    return diagnostics


def _rolling_ic_weights(
    factors: dict[str, pd.DataFrame],
    future_return: pd.DataFrame,
    *,
    lookback: int = 252,
    label_horizon: int = 5,
) -> pd.DataFrame:
    ic_frame = pd.DataFrame(
        {name: _cross_sectional_ic(factor, future_return) for name, factor in factors.items()}
    ).sort_index()
    rolling = ic_frame.rolling(lookback, min_periods=80).mean().shift(label_horizon)
    return rolling.reindex(future_return.index)


def _industry_demean(score: pd.Series, industry_map: dict[str, str]) -> pd.Series:
    data = score.dropna().copy()
    if data.empty or not industry_map:
        return data
    industries = pd.Series({symbol: industry_map.get(symbol, "__UNKNOWN__") for symbol in data.index})
    medians = data.groupby(industries).transform("median")
    return data - medians


def _build_market_regime_features(close: pd.DataFrame) -> dict[str, np.ndarray]:
    """Precompute market regime inputs once for all candidate and WF runs."""
    market = close.pct_change(fill_method=None).mean(axis=1).fillna(0.0)
    equity = (1.0 + market).cumprod()
    return {
        "mom_20": (equity / equity.shift(20) - 1.0).to_numpy(dtype=float),
        "mom_60": (equity / equity.shift(60) - 1.0).to_numpy(dtype=float),
        "vol_20": (market.rolling(20, min_periods=20).std(ddof=1) * np.sqrt(252)).to_numpy(dtype=float),
    }


def _market_regime(regime: dict[str, np.ndarray], t: int, config: dict[str, Any]) -> tuple[float, bool, float]:
    if t < 120:
        return float(config.get("neutral_gross", 0.5)), True, 0.3
    mom_20 = float(regime["mom_20"][t])
    mom_60 = float(regime["mom_60"][t])
    vol_20 = float(regime["vol_20"][t])
    if not math.isfinite(mom_20):
        mom_20 = 0.0
    if not math.isfinite(mom_60):
        mom_60 = 0.0
    if not math.isfinite(vol_20):
        vol_20 = 0.3
    risk_off = mom_20 < -0.03 or mom_60 < -0.08 or vol_20 > 0.34
    if risk_off:
        return float(config.get("risk_off_gross", 0.35)), True, vol_20
    if mom_20 > 0.03 and mom_60 > 0.02 and vol_20 < 0.28:
        return float(config.get("bull_gross", 1.0)), False, vol_20
    return float(config.get("neutral_gross", 0.65)), False, vol_20


def _candidate_configs() -> list[dict[str, Any]]:
    return [
        {
            "name": "v28_reversal_liquidity_crisis_freq5",
            "mode": "static",
            "factor_weights": {
                "reversal_5d": 0.30,
                "reversal_1d": 0.15,
                "low_vol_20d": 0.20,
                "liquidity_20d": 0.20,
                "volume_stability_20d": 0.15,
            },
            "rebalance_freq": 5,
            "top_n": 40,
            "max_per_industry": 4,
            "cost_bps": 12,
            "use_crisis_sleeve": True,
        },
        {
            "name": "v28_reversal_liquidity_no_crisis_freq5",
            "mode": "static",
            "factor_weights": {
                "reversal_5d": 0.35,
                "low_vol_20d": 0.25,
                "liquidity_20d": 0.25,
                "volume_stability_20d": 0.15,
            },
            "rebalance_freq": 5,
            "top_n": 40,
            "max_per_industry": 4,
            "cost_bps": 12,
            "use_crisis_sleeve": False,
        },
        {
            "name": "v28_ic_adaptive_crisis_freq5",
            "mode": "ic_adaptive",
            "factor_set": "price_only",
            "rebalance_freq": 5,
            "top_n": 50,
            "max_per_industry": 5,
            "cost_bps": 12,
            "use_crisis_sleeve": True,
        },
        {
            "name": "v28_ic_adaptive_crisis_freq10",
            "mode": "ic_adaptive",
            "factor_set": "price_only",
            "rebalance_freq": 10,
            "top_n": 60,
            "max_per_industry": 6,
            "cost_bps": 10,
            "use_crisis_sleeve": True,
        },
        {
            "name": "v28_ic_adaptive_crisis_dd_cap_freq10",
            "mode": "ic_adaptive",
            "factor_set": "price_only",
            "rebalance_freq": 10,
            "top_n": 60,
            "max_per_industry": 6,
            "cost_bps": 10,
            "use_crisis_sleeve": True,
            "risk_off_gross": 0.20,
            "neutral_gross": 0.55,
            "bull_gross": 0.90,
            "dd_reduce_threshold": 0.06,
            "dd_reduce_scale": 0.35,
            "dd_stop_threshold": 0.12,
            "dd_stop_scale": 0.05,
            "vol_target": 0.16,
        },
        {
            "name": "v28_ic_adaptive_crisis_lowvol_freq10",
            "mode": "ic_adaptive",
            "factor_set": "price_only",
            "rebalance_freq": 10,
            "top_n": 70,
            "max_per_industry": 7,
            "cost_bps": 10,
            "use_crisis_sleeve": True,
            "risk_off_gross": 0.12,
            "neutral_gross": 0.40,
            "bull_gross": 0.72,
            "dd_reduce_threshold": 0.06,
            "dd_reduce_scale": 0.35,
            "dd_stop_threshold": 0.11,
            "dd_stop_scale": 0.05,
            "vol_target": 0.12,
        },
        {
            "name": "v28_ic_adaptive_crisis_balanced_freq10",
            "mode": "ic_adaptive",
            "factor_set": "price_only",
            "rebalance_freq": 10,
            "top_n": 55,
            "max_per_industry": 6,
            "cost_bps": 10,
            "use_crisis_sleeve": True,
            "risk_off_gross": 0.18,
            "neutral_gross": 0.52,
            "bull_gross": 0.95,
            "dd_reduce_threshold": 0.07,
            "dd_reduce_scale": 0.40,
            "dd_stop_threshold": 0.13,
            "dd_stop_scale": 0.08,
            "vol_target": 0.15,
        },
        {
            "name": "v28_momentum_lowvol_crisis_freq20",
            "mode": "static",
            "factor_weights": {
                "momentum_60d": 0.35,
                "momentum_20d": 0.20,
                "low_vol_60d": 0.25,
                "liquidity_20d": 0.20,
            },
            "rebalance_freq": 20,
            "top_n": 60,
            "max_per_industry": 6,
            "cost_bps": 8,
            "use_crisis_sleeve": True,
        },
        {
            "name": "v28_lowvol_liquidity_crisis_freq10",
            "mode": "static",
            "factor_weights": {
                "low_vol_20d": 0.35,
                "low_vol_60d": 0.25,
                "liquidity_20d": 0.25,
                "volume_stability_20d": 0.15,
            },
            "rebalance_freq": 10,
            "top_n": 80,
            "max_per_industry": 8,
            "cost_bps": 8,
            "use_crisis_sleeve": True,
        },
        {
            "name": "v28_ic_adaptive_guarded_freq10",
            "mode": "ic_adaptive",
            "factor_set": "price_only",
            "rebalance_freq": 10,
            "top_n": 70,
            "max_per_industry": 7,
            "cost_bps": 10,
            "use_crisis_sleeve": True,
            "risk_off_gross": 0.20,
            "neutral_gross": 0.50,
            "bull_gross": 0.85,
            "dd_reduce_threshold": 0.08,
            "dd_reduce_scale": 0.50,
            "dd_stop_threshold": 0.14,
            "dd_stop_scale": 0.15,
            "vol_target": 0.16,
        },
        {
            "name": "v28_ic_adaptive_defensive_freq20",
            "mode": "ic_adaptive",
            "factor_set": "price_only",
            "rebalance_freq": 20,
            "top_n": 90,
            "max_per_industry": 9,
            "cost_bps": 8,
            "use_crisis_sleeve": True,
            "risk_off_gross": 0.15,
            "neutral_gross": 0.45,
            "bull_gross": 0.75,
            "dd_reduce_threshold": 0.07,
            "dd_reduce_scale": 0.45,
            "dd_stop_threshold": 0.12,
            "dd_stop_scale": 0.10,
            "vol_target": 0.14,
        },
        {
            "name": "v28_anti_momentum_lowvol_guarded_freq10",
            "mode": "static",
            "factor_weights": {
                "reversal_5d": 0.20,
                "momentum_20d": -0.20,
                "momentum_60d": -0.20,
                "low_vol_20d": 0.20,
                "low_vol_60d": 0.15,
                "liquidity_20d": -0.10,
                "volume_stability_20d": 0.15,
            },
            "rebalance_freq": 10,
            "top_n": 70,
            "max_per_industry": 7,
            "cost_bps": 10,
            "use_crisis_sleeve": True,
            "risk_off_gross": 0.20,
            "neutral_gross": 0.50,
            "bull_gross": 0.85,
            "dd_reduce_threshold": 0.08,
            "dd_reduce_scale": 0.50,
            "dd_stop_threshold": 0.14,
            "dd_stop_scale": 0.15,
            "vol_target": 0.16,
        },
        {
            "name": "v28_ic_adaptive_market_neutral_freq5",
            "mode": "ic_adaptive",
            "factor_set": "price_only",
            "rebalance_freq": 5,
            "top_n": 60,
            "max_per_industry": 6,
            "cost_bps": 14,
            "use_crisis_sleeve": False,
            "market_neutral": True,
            "gross_exposure": 1.0,
            "production_comparable": False,
            "production_blocker": "synthetic long-short requires real broker-backed borrow availability",
        },
        {
            "name": "v28_ic_adaptive_market_neutral_freq10",
            "mode": "ic_adaptive",
            "factor_set": "price_only",
            "rebalance_freq": 10,
            "top_n": 80,
            "max_per_industry": 8,
            "cost_bps": 12,
            "use_crisis_sleeve": False,
            "market_neutral": True,
            "gross_exposure": 1.0,
            "production_comparable": False,
            "production_blocker": "synthetic long-short requires real broker-backed borrow availability",
        },
        {
            "name": "v28_alt_ic_adaptive_guarded_freq10",
            "mode": "ic_adaptive",
            "factor_set": "all",
            "rebalance_freq": 10,
            "top_n": 70,
            "max_per_industry": 7,
            "cost_bps": 10,
            "use_crisis_sleeve": True,
            "risk_off_gross": 0.20,
            "neutral_gross": 0.50,
            "bull_gross": 0.85,
            "dd_reduce_threshold": 0.08,
            "dd_reduce_scale": 0.50,
            "dd_stop_threshold": 0.14,
            "dd_stop_scale": 0.15,
            "vol_target": 0.16,
            "production_comparable": False,
            "production_blocker": "alt-enhanced candidate still relies on snapshot/archived research panels, not production PIT entitlement",
        },
        {
            "name": "v28_alt_ic_adaptive_market_neutral_freq10",
            "mode": "ic_adaptive",
            "factor_set": "all",
            "rebalance_freq": 10,
            "top_n": 80,
            "max_per_industry": 8,
            "cost_bps": 12,
            "use_crisis_sleeve": False,
            "market_neutral": True,
            "gross_exposure": 1.0,
            "production_comparable": False,
            "production_blocker": "synthetic long-short plus alt panels require real borrow and PIT entitlement evidence",
        },
    ]


def _score_static(factors: dict[str, pd.DataFrame], t: int, weights: dict[str, float]) -> pd.Series:
    columns = next(iter(factors.values())).columns
    score = np.zeros(len(columns), dtype=float)
    total = np.zeros(len(columns), dtype=float)
    for name, weight in weights.items():
        z = _zscore_array(factors[name].iloc[t])
        if z is None:
            continue
        mask = np.isfinite(z)
        if not mask.any():
            continue
        score[mask] += float(weight) * z[mask]
        total[mask] += abs(float(weight))
    out = np.full(len(columns), np.nan, dtype=float)
    np.divide(score, total, out=out, where=total > 0.0)
    return pd.Series(out, index=columns, dtype=float).dropna()


def _score_ic_adaptive(
    factors: dict[str, pd.DataFrame],
    rolling_ic: pd.DataFrame,
    t: int,
    factor_names: set[str] | None = None,
) -> pd.Series:
    date = next(iter(factors.values())).index[t]
    if date not in rolling_ic.index:
        return pd.Series(dtype=float)
    ic = rolling_ic.loc[date].dropna().clip(-0.04, 0.04)
    if factor_names is not None:
        ic = ic[ic.index.isin(factor_names)]
    ic = ic[ic.abs() >= 0.0025]
    if ic.empty:
        return pd.Series(dtype=float)
    columns = next(iter(factors.values())).columns
    score = np.zeros(len(columns), dtype=float)
    total_abs = np.zeros(len(columns), dtype=float)
    for name, weight in ic.items():
        z = _zscore_array(factors[name].iloc[t])
        if z is None:
            continue
        mask = np.isfinite(z)
        if not mask.any():
            continue
        score[mask] += float(weight) * z[mask]
        total_abs[mask] += abs(float(weight))
    out = np.full(len(columns), np.nan, dtype=float)
    np.divide(score, total_abs, out=out, where=total_abs > 0.0)
    return pd.Series(out, index=columns, dtype=float).dropna()


def _select_weights(
    score: pd.Series,
    industry_map: dict[str, str],
    *,
    top_n: int,
    max_per_industry: int,
    gross: float,
    market_neutral: bool = False,
) -> dict[str, float]:
    adjusted = _industry_demean(score, industry_map).replace([np.inf, -np.inf], np.nan).dropna()
    if adjusted.empty or gross <= 0:
        return {}
    def select_from(ranked_symbols: pd.Index, excluded: set[str] | None = None) -> list[str]:
        selected_items: list[str] = []
        counts: dict[str, int] = {}
        excluded = excluded or set()
        for symbol_value in ranked_symbols:
            symbol = str(symbol_value)
            if symbol in excluded:
                continue
            industry = industry_map.get(symbol, "__UNKNOWN__")
            if counts.get(industry, 0) >= max_per_industry:
                continue
            selected_items.append(symbol)
            counts[industry] = counts.get(industry, 0) + 1
            if len(selected_items) >= top_n:
                break
        return selected_items

    ranked = adjusted.sort_values(ascending=False)
    selected = select_from(ranked.index)
    if not market_neutral:
        if not selected:
            return {}
        weight = gross / len(selected)
        return {symbol: weight for symbol in selected}

    short_selected = select_from(adjusted.sort_values(ascending=True).index, set(selected))
    if not selected or not short_selected:
        return {}
    long_weight = gross * 0.5 / len(selected)
    short_weight = -gross * 0.5 / len(short_selected)
    weights = {symbol: long_weight for symbol in selected}
    weights.update({symbol: short_weight for symbol in short_selected})
    return weights


def _metrics(returns: list[float]) -> dict[str, Any]:
    series = pd.Series(returns, dtype=float)
    if len(series) < 2:
        return {"error": "not enough returns"}
    equity = (1.0 + series).cumprod()
    ann_ret = float(series.mean() * 252)
    ann_vol = float(series.std(ddof=1) * np.sqrt(252)) if len(series) > 1 else 0.0
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


def _backtest(
    config: dict[str, Any],
    close: pd.DataFrame,
    daily_stock_returns: pd.DataFrame,
    regime: dict[str, np.ndarray],
    factors: dict[str, pd.DataFrame],
    rolling_ic: pd.DataFrame,
    industry_map: dict[str, str],
    crisis_returns: pd.Series,
    *,
    start_idx: int,
    end_idx: int,
    metrics_start_idx: int | None = None,
) -> dict[str, Any]:
    positions: dict[str, float] = {}
    crisis_weight = 0.0
    returns: list[float] = []
    turnovers: list[float] = []
    stock_exposures: list[float] = []
    gross_exposures: list[float] = []
    active_counts: list[int] = []
    risk_off_days = 0
    cost_rate = float(config["cost_bps"]) / 10000.0
    equity = 1.0
    peak_equity = 1.0
    for t in range(max(start_idx, 252), end_idx - 1):
        if (t - max(start_idx, 252)) % int(config["rebalance_freq"]) == 0:
            gross, risk_off, market_vol = _market_regime(regime, t, config)
            if bool(config.get("market_neutral")):
                gross = float(config.get("gross_exposure", gross))
            vol_target = config.get("vol_target")
            if not bool(config.get("market_neutral")) and isinstance(vol_target, int | float) and market_vol > 1e-8:
                gross *= min(1.0, float(vol_target) / market_vol)
            current_drawdown = (equity - peak_equity) / peak_equity if peak_equity > 0 else 0.0
            if current_drawdown < -float(config.get("dd_stop_threshold", 1.0)):
                gross *= float(config.get("dd_stop_scale", 0.0))
            elif current_drawdown < -float(config.get("dd_reduce_threshold", 1.0)):
                gross *= float(config.get("dd_reduce_scale", 0.5))
            gross = max(0.0, min(1.0, gross))
            risk_off_days += int(risk_off)
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
                gross=gross,
                market_neutral=bool(config.get("market_neutral")),
            )
            new_crisis = (
                (1.0 - gross)
                if bool(config["use_crisis_sleeve"]) and not bool(config.get("market_neutral"))
                else 0.0
            )
            symbols = set(positions) | set(new_positions)
            turnover = sum(abs(new_positions.get(sym, 0.0) - positions.get(sym, 0.0)) for sym in symbols)
            turnover += abs(new_crisis - crisis_weight)
            positions = new_positions
            crisis_weight = new_crisis
        else:
            turnover = 0.0
        next_returns = daily_stock_returns.iloc[t + 1]
        stock_return = sum(weight * float(next_returns.get(symbol, 0.0)) for symbol, weight in positions.items())
        sleeve_return = crisis_weight * float(crisis_returns.iloc[t + 1])
        net_return = stock_return + sleeve_return - turnover * cost_rate
        equity *= 1.0 + net_return
        peak_equity = max(peak_equity, equity)
        if metrics_start_idx is None or t + 1 >= metrics_start_idx:
            returns.append(net_return)
            turnovers.append(turnover)
            stock_exposures.append(sum(positions.values()))
            gross_exposures.append(sum(abs(weight) for weight in positions.values()) + abs(crisis_weight))
            active_counts.append(len(positions))
    out = _metrics(returns)
    out.update(
        {
            "avg_turnover": round(float(np.mean(turnovers)), 4) if turnovers else 0.0,
            "avg_stock_exposure": round(float(np.mean(stock_exposures)), 4) if stock_exposures else 0.0,
            "avg_gross_exposure": round(float(np.mean(gross_exposures)), 4) if gross_exposures else 0.0,
            "avg_active_stocks": round(float(np.mean(active_counts)), 2) if active_counts else 0.0,
            "max_active_stocks": int(max(active_counts)) if active_counts else 0,
            "risk_off_rebalances": risk_off_days,
        }
    )
    return out


def _walk_forward(
    config: dict[str, Any],
    close: pd.DataFrame,
    daily_stock_returns: pd.DataFrame,
    regime: dict[str, np.ndarray],
    factors: dict[str, pd.DataFrame],
    rolling_ic: pd.DataFrame,
    industry_map: dict[str, str],
    crisis_returns: pd.Series,
) -> dict[str, Any]:
    warmup = 756
    test_days = 252
    folds: list[dict[str, Any]] = []
    latest_end = len(close) - 1
    fold_starts = [latest_end - test_days * idx for idx in range(5, 0, -1)]
    for fold, test_start in enumerate(fold_starts):
        test_end = min(test_start + test_days, latest_end)
        context_start = max(0, test_start - warmup)
        is_result = _backtest(
            config,
            close,
            daily_stock_returns,
            regime,
            factors,
            rolling_ic,
            industry_map,
            crisis_returns,
            start_idx=context_start,
            end_idx=test_start,
        )
        oos_result = _backtest(
            config,
            close,
            daily_stock_returns,
            regime,
            factors,
            rolling_ic,
            industry_map,
            crisis_returns,
            start_idx=context_start,
            end_idx=test_end,
            metrics_start_idx=test_start,
        )
        folds.append(
            {
                "fold": fold,
                "test_start": close.index[test_start].date().isoformat(),
                "test_end": close.index[test_end].date().isoformat(),
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
        "folds": folds,
        "avg_is_sharpe": round(avg_is, 4),
        "avg_oos_sharpe": round(avg_oos, 4),
        "sharpe_decay": round(decay, 4),
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "name",
        "sharpe_ratio",
        "annual_return",
        "annual_volatility",
        "max_drawdown",
        "win_rate",
        "total_return",
        "avg_oos_sharpe",
        "sharpe_decay",
        "avg_stock_exposure",
        "avg_active_stocks",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            flat = {
                "name": row["name"],
                **{key: row["full"].get(key) for key in fields if key not in {"name", "avg_oos_sharpe", "sharpe_decay"}},
                "avg_oos_sharpe": row["wf"].get("avg_oos_sharpe"),
                "sharpe_decay": row["wf"].get("sharpe_decay"),
            }
            writer.writerow(flat)


def main() -> None:
    args = _parse_args()
    started = time.perf_counter()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    universe_path = Path(args.universe)
    codes = _read_codes(universe_path)
    close, volume, amount = _load_market_panel(codes, min_history=args.min_history)
    start = pd.Timestamp(datetime.strptime(args.start_date, "%Y%m%d")).normalize()
    end = pd.Timestamp(datetime.strptime(args.end_date, "%Y%m%d")).normalize()
    close = close.loc[(close.index >= start) & (close.index <= end)].copy()
    volume = volume.reindex(close.index)
    amount = amount.reindex(close.index)
    daily_stock_returns = close.pct_change(fill_method=None).fillna(0.0)
    regime = _build_market_regime_features(close)
    factors = _build_factors(close, volume, amount)
    alt_factors, alt_summary = _load_alt_factors(Path(args.alt_feature_file), close.index, close.columns)
    factors.update(alt_factors)
    future_5d = close.shift(-5) / close - 1.0
    diagnostics = _factor_diagnostics(factors, future_5d)
    rolling_ic = _rolling_ic_weights(factors, future_5d, label_horizon=5)
    industry_map = _load_industry_map()
    industry_overlap = len(set(close.columns) & set(industry_map))
    crisis_returns = _load_crisis_returns(close.index)
    results: list[dict[str, Any]] = []
    for config in _candidate_configs():
        full = _backtest(
            config,
            close,
            daily_stock_returns,
            regime,
            factors,
            rolling_ic,
            industry_map,
            crisis_returns,
            start_idx=0,
            end_idx=len(close) - 1,
        )
        wf = _walk_forward(
            config,
            close,
            daily_stock_returns,
            regime,
            factors,
            rolling_ic,
            industry_map,
            crisis_returns,
        )
        result = {
            "name": config["name"],
            "version": "V28-factor-direction",
            "production_comparable": bool(config.get("production_comparable", True)),
            "production_blockers": [config["production_blocker"]] if config.get("production_blocker") else [],
            "config": config,
            "full": full,
            "wf": wf,
            "gates": {
                "S": full.get("sharpe_ratio", -999) >= 1.2,
                "M": full.get("max_drawdown", -999) >= -0.15,
                "W": wf.get("avg_oos_sharpe", -999) >= 0.84,
                "D": wf.get("sharpe_decay", 999) <= 0.30,
            },
        }
        results.append(result)
    results.sort(
        key=lambda item: (
            float(item["wf"].get("avg_oos_sharpe", -999)),
            float(item["full"].get("sharpe_ratio", -999)),
        ),
        reverse=True,
    )
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V28-factor-direction",
        "research_only": True,
        "production_ready": False,
        "universe": {
            "path": str(universe_path),
            "size": len(codes),
            "sha256": _sha256_file(universe_path),
        },
        "panel": {
            "start": close.index.min().date().isoformat(),
            "end": close.index.max().date().isoformat(),
            "days": len(close),
            "symbols": len(close.columns),
            "industry_overlap": industry_overlap,
        },
        "alt_feature_summary": alt_summary,
        "factor_diagnostics": diagnostics,
        "results": results,
        "best": results[0] if results else None,
        "production_blockers": [
            "research-only daily-bar probe; not paper/live evidence",
            "uses public/local data and still requires PIT alt history and external evidence",
            "must pass full production scorecard, paper trading and approval before promotion",
        ],
        "elapsed_s": round(time.perf_counter() - started, 3),
    }
    output_json = Path(args.output_json)
    output_csv = Path(args.output_csv)
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(output_csv, results)
    best = payload["best"] or {}
    full = best.get("full", {})
    wf = best.get("wf", {})
    print(f"Wrote {output_json}")
    print(f"Wrote {output_csv}")
    print(
        "Best V28: "
        f"{best.get('name')} full_sharpe={full.get('sharpe_ratio')} "
        f"mdd={full.get('max_drawdown')} "
        f"oos={wf.get('avg_oos_sharpe')} "
        f"decay={wf.get('sharpe_decay')}"
    )


if __name__ == "__main__":
    main()
