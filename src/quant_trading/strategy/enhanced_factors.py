"""
Enhanced multi-factor model with fundamental and price-volume factors.

New factors:
  Fundamental: pe_percentile, pb_percentile, ps_percentile (cross-sectional ranking)
  Price-volume: turnover_anomaly, money_flow, volume_price_trend

These are added to the existing MR factors (rsi, bollinger, momentum, macd, vol_dev, low_vol).
"""

from __future__ import annotations

import numpy as np


# ── Fundamental Factors ──────────────────────────────────────
def factor_pe_percentile(pe_value: float | None) -> float:
    """PE percentile: lower PE = cheaper = better.
    Returns NaN if no data. Will be z-scored cross-sectionally."""
    if pe_value is None or np.isnan(pe_value) or pe_value <= 0:
        return np.nan
    return float(-np.log(pe_value))  # log scale: lower PE → higher score


def factor_pb_percentile(pb_value: float | None) -> float:
    """PB percentile: lower PB = cheaper = better."""
    if pb_value is None or np.isnan(pb_value) or pb_value <= 0:
        return np.nan
    return float(-np.log(pb_value))


def factor_ps_percentile(ps_value: float | None) -> float:
    """PS percentile: lower PS = cheaper = better."""
    if ps_value is None or np.isnan(ps_value) or ps_value <= 0:
        return np.nan
    return float(-np.log(ps_value))


# ── Price-Volume Factors ─────────────────────────────────────
def factor_turnover_anomaly(turnover: np.ndarray, window: int = 20) -> float:
    """Turnover anomaly: current turnover / 20d avg - 1.
    Positive = unusually high turnover (attention signal)."""
    if len(turnover) < window:
        return np.nan
    recent = turnover[-5:].mean()
    base = turnover[-window:-5].mean()
    if base < 1e-12:
        return 0.0
    return float(recent / base - 1.0)


def factor_money_flow(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    volumes: np.ndarray,
    window: int = 5,
) -> float:
    """Chaikin Money Flow: measures buying/selling pressure.
    Positive = accumulation (bullish), Negative = distribution (bearish)."""
    if len(closes) < window + 1:
        return np.nan

    # Typical price
    high = highs[-window - 1 :]
    low = lows[-window - 1 :]
    close = closes[-window - 1 :]
    vol = volumes[-window - 1 :]

    hl_diff = high - low
    # Avoid division by zero
    hl_diff = np.where(hl_diff < 1e-12, 1e-12, hl_diff)

    # Money Flow Multiplier
    mfm = ((close - low) - (high - close)) / hl_diff
    # Money Flow Volume
    mfv = mfm * vol

    # Chaikin Money Flow = sum(MFV over N) / sum(volume over N)
    cmf = np.sum(mfv[-window:]) / max(np.sum(vol[-window:]), 1e-12)
    return float(cmf)


def factor_volume_price_trend(closes: np.ndarray, volumes: np.ndarray, window: int = 5) -> float:
    """Volume-Price Trend: cumulative volume * price change.
    Rising = bullish confirmation (price up on volume)."""
    if len(closes) < window + 1:
        return np.nan

    close = closes[-window - 1 :]
    vol = volumes[-window - 1 :]

    pct_changes = np.diff(close) / close[:-1]
    vpt = np.sum(vol[1:] * pct_changes)

    # Normalize by total volume
    total_vol = np.sum(vol[1:])
    if total_vol < 1e-12:
        return 0.0
    return float(vpt / total_vol)


def factor_relative_strength(
    closes: np.ndarray, benchmark_closes: np.ndarray | None = None, window: int = 63
) -> float:
    """Relative strength vs benchmark (or self): recent return / longer return.
    Higher = stronger recent momentum vs historical."""
    if len(closes) < window + 10:
        return np.nan

    # Self-relative: recent 10d return / 63d return
    recent_ret = closes[-1] / closes[-10] - 1.0 if len(closes) >= 10 else 0.0
    long_ret = closes[-1] / closes[-window] - 1.0
    if abs(long_ret) < 1e-12:
        return 0.0 if abs(recent_ret) < 1e-12 else np.sign(recent_ret)
    return float(recent_ret / abs(long_ret))


# ── Enhanced Factor Weights (no fundamentals yet) ────────────
ENHANCED_WEIGHTS = {
    # Mean-reversion (core)
    "rsi": 0.18,
    "bollinger": 0.18,
    "momentum": 0.14,
    "macd": 0.10,
    "vol_dev": 0.08,
    "low_vol": 0.05,
    # Price-volume
    "turnover": 0.10,
    "money_flow": 0.07,
    "vpt": 0.05,
    "rel_strength": 0.05,
}

FACTOR_NAMES = list(ENHANCED_WEIGHTS.keys())
