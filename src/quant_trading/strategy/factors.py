"""
Multi-factor model for A-share cross-sectional ranking.

Factors adapted from quant-ashare-dev with IC-derived weights.
All factor signals are computed per-stock, then cross-sectionally
z-scored and IC-weighted to produce a composite score.
"""
from __future__ import annotations

import numpy as np

# ── Factor Config ─────────────────────────────────────────────
# Weights from CSI300 rolling 6-month Spearman IC analysis
# Key insight: in large-cap A-shares, trend-following dominates mean-reversion
FACTOR_WEIGHTS = {
    "bollinger": 0.278,     # trend > mean-reversion (neg IC = extremes outperform)
    "rsi":       0.277,     # same — RSI away from 50 predicts continuation
    "momentum":  0.263,     # positive momentum works in large-caps
    "macd":      0.130,     # trend confirmation
    "vol_dev":   0.047,     # weak signal
    "low_vol":   0.005,     # weak in CSI 300
}

# ── Helper ────────────────────────────────────────────────────
def _safe_div(a, b, fill=0.0):
    """Divide with NaN handling."""
    if isinstance(b, np.ndarray):
        b = np.where(np.abs(b) < 1e-12, np.nan, b)
        result = a / b
        return np.where(np.isnan(result), fill, result)
    return a / b if abs(b) > 1e-12 else fill


# ── Factor Functions ──────────────────────────────────────────
def factor_rsi(closes: np.ndarray, period: int = 14) -> float:
    """RSI(14): signal = -|RSI - 50| (closer to 0 = neutral).
    
    Negative IC means extremes outperform — we multiply by -1 so
    higher score = better in cross-sectional ranking.
    """
    if len(closes) < period + 1:
        return np.nan
    delta = np.diff(closes[-period-1:])
    gain = np.clip(delta, 0, None).mean()
    loss = -np.clip(delta, None, 0).mean()
    if loss < 1e-12:
        return np.nan
    rs = gain / loss
    rsi = 100.0 - 100.0 / (1.0 + rs)
    # IC = -0.04 → extremes outperform → -|RSI-50| naturally lower for extremes
    # But since we want higher=better for ranking, and original signal = -|RSI-50|
    # which is ≤0, we keep as-is and rely on IC-weighted sum direction
    return -abs(rsi - 50.0)


def factor_macd(closes: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9) -> float:
    """MACD histogram: DIF - DEA. Positive = bullish."""
    if len(closes) < slow + signal:
        return np.nan
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    dif = ema_fast - ema_slow
    dea = _ema(np.array([dif]), signal) if not isinstance(dif, np.ndarray) else _ema_single_series(dif, signal)
    if isinstance(dif, np.ndarray):
        return float(dif[-1] - dea[-1])
    return dif - dea


def _ema(series: np.ndarray, span: int) -> np.ndarray:
    """Exponential moving average."""
    if len(series) < span:
        return np.full(len(series), np.nan)
    # Use pandas-style alpha = 2/(span+1)
    alpha = 2.0 / (span + 1.0)
    result = np.full(len(series), np.nan)
    result[span-1] = series[:span].mean()
    for i in range(span, len(result)):
        result[i] = alpha * series[i] + (1 - alpha) * result[i-1]
    return result


def _ema_single_series(series: np.ndarray, span: int) -> np.ndarray:
    """EMA on a 1D series that may be shorter."""
    if len(series) < span:
        return np.full(len(series), np.nan)
    alpha = 2.0 / (span + 1.0)
    result = np.full(len(series), np.nan)
    result[span-1] = np.mean(series[:span])
    for i in range(span, len(result)):
        result[i] = alpha * series[i] + (1 - alpha) * result[i-1]
    return result


def factor_bollinger(closes: np.ndarray, window: int = 20, num_std: float = 2.0) -> float:
    """Bollinger: -|close - MA| / bandwidth. Closer to 0 = near band center.
    
    Negative IC means extreme positions (far from band) outperform,
    so we take the raw signal directly — it's already negative for extremes.
    IC-weighted sum handles direction.
    """
    if len(closes) < window:
        return np.nan
    ma = closes[-window:].mean()
    std = closes[-window:].std(ddof=1)
    upper = ma + num_std * std
    lower = ma - num_std * std
    width = upper - lower
    if width < 1e-12:
        return 0.0
    close = closes[-1]
    return -abs(close - ma) / width


def factor_momentum(closes: np.ndarray, lookback: int = 63, skip: int = 5) -> float:
    """Momentum: 3-month return skipping 1 week (避免短期反转)."""
    if len(closes) < lookback + skip:
        return np.nan
    return float(closes[-1] / closes[-lookback-1] - 1.0)


def factor_vol_dev(volumes: np.ndarray, window: int = 20) -> float:
    """Volume deviation: -|vol / vol_ma - 1|. Closer to 0 = normal volume."""
    if len(volumes) < window:
        return np.nan
    ma = volumes[-window:].mean()
    if ma < 1e-12:
        return 0.0
    return -abs(volumes[-1] / ma - 1.0)


def factor_low_vol(closes: np.ndarray, window: int = 20) -> float:
    """Low volatility: -std daily returns. Higher = lower vol.
    
    Returns -1 * annualized daily vol so higher score = lower risk.
    """
    if len(closes) < window + 1:
        return np.nan
    rets = np.diff(closes[-window-1:]) / closes[-window-1:-1]
    vol = np.std(rets, ddof=1) * np.sqrt(252)
    return -vol  # negative vol → higher is lower risk


# ── Factor Registry ───────────────────────────────────────────
FACTOR_REGISTRY = {
    "rsi":       (factor_rsi,       "close"),
    "macd":      (factor_macd,      "close"),
    "bollinger": (factor_bollinger, "close"),
    "momentum":  (factor_momentum,  "close"),
    "vol_dev":   (factor_vol_dev,   "volume"),
    "low_vol":   (factor_low_vol,   "close"),
}


def compute_factor_scores(
    closes: np.ndarray,
    volumes: np.ndarray | None = None,
    factors: list[str] | None = None,
    weights: dict[str, float] | None = None,
) -> dict[str, float]:
    """Compute factor values for one stock at current timestamp.
    
    Returns {factor_name: raw_value} dict.
    NaN for insufficient data.
    """
    if factors is None:
        factors = list(FACTOR_REGISTRY.keys())
    if weights is None:
        weights = FACTOR_WEIGHTS
    
    scores = {}
    for name in factors:
        func, data_type = FACTOR_REGISTRY[name]
        if data_type == "close":
            scores[name] = func(closes)
        elif data_type == "volume" and volumes is not None:
            scores[name] = func(volumes)
        else:
            scores[name] = np.nan
    return scores


def composite_score(
    factor_values: dict[str, float],
    weights: dict[str, float] | None = None,
    cross_sectional_z: dict[str, tuple[float, float]] | None = None,
) -> float:
    """Compute IC-weighted composite score from factor values.
    
    If cross_sectional_z is provided, z-score each factor before weighting.
    cross_sectional_z: {factor_name: (mean, std)} across all stocks.
    """
    if weights is None:
        weights = FACTOR_WEIGHTS
    
    score = 0.0
    total_weight = 0.0
    for name, w in weights.items():
        v = factor_values.get(name, np.nan)
        if np.isnan(v):
            continue
        
        # Apply cross-sectional z-score if available
        if cross_sectional_z and name in cross_sectional_z:
            mu, sigma = cross_sectional_z[name]
            if sigma > 1e-12:
                v = (v - mu) / sigma
        
        score += w * v
        total_weight += w
    
    if total_weight < 1e-12:
        return np.nan
    return score / total_weight  # normalize by sum of valid weights


def rank_stocks(
    stock_data: dict[str, dict],
    factors: list[str] | None = None,
    weights: dict[str, float] | None = None,
) -> list[tuple[str, float]]:
    """Cross-sectional ranking of stocks by composite factor score.
    
    stock_data: {symbol: {"close": np.array, "volume": np.array}, ...}
    Returns: [(symbol, composite_score), ...] sorted descending.
    """
    if factors is None:
        factors = list(FACTOR_REGISTRY.keys())
    if weights is None:
        weights = FACTOR_WEIGHTS
    
    # Step 1: compute raw factor values per stock
    raw_factors: dict[str, dict[str, float]] = {}
    for sym, data in stock_data.items():
        closes = np.asarray(data["close"])
        volumes = np.asarray(data.get("volume", []))
        fv = compute_factor_scores(closes, volumes, factors, weights)
        if not all(np.isnan(v) for v in fv.values()):
            raw_factors[sym] = fv
    
    if not raw_factors:
        return []
    
    # Step 2: compute cross-sectional mean/std for z-scoring
    cross_z = {}
    for name in factors:
        vals = [fv.get(name, np.nan) for fv in raw_factors.values()]
        valid = [v for v in vals if not np.isnan(v)]
        if len(valid) >= 5:
            cross_z[name] = (float(np.mean(valid)), float(np.std(valid, ddof=1)))
    
    # Step 3: compute composite scores
    ranked = []
    for sym, fv in raw_factors.items():
        cs = composite_score(fv, weights, cross_z)
        if not np.isnan(cs):
            ranked.append((sym, cs))
    
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked


# ── Quick test ────────────────────────────────────────────────
if __name__ == "__main__":
    import pandas as pd
    from pathlib import Path
    
    DATA_DIR = Path("/home/hermes/.hermes/projects/lianghua/data/parquet")
    files = sorted(DATA_DIR.glob("*.parquet"))[:50]
    
    stock_data = {}
    for f in files:
        df = pd.read_parquet(f)
        stock_data[f.stem] = {
            "close": df["close"].values[-252:],
            "volume": df["volume"].values[-252:] if "volume" in df else df.get("vol", pd.Series()).values[-252:],
        }
    
    ranked = rank_stocks(stock_data)
    print(f"Ranked {len(ranked)} stocks")
    for sym, score in ranked[:10]:
        print(f"  {sym}: {score:.4f}")
    if ranked:
        print(f"  ...")
        for sym, score in ranked[-3:]:
            print(f"  {sym}: {score:.4f}")
