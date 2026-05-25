"""
V6 Extended Factor Library — 30+ factors across 7 style categories.
Industry-standard quant factor taxonomy (WorldQuant/Barra/Two Sigma style).

Categories:
  Value (3)    — E/P, B/P, S/P (from fundamental data)
  Momentum (7) — short/medium/long momentum, RSI, MACD, Bollinger
  Reversal (3) — 1d, 1w, 1m reversal
  Volatility (5)— realized vol, idiosyncratic vol, beta, max ret, skew
  Liquidity (5)— turnover, Amihud illiquidity, dollar volume
  Size (1)     — log market cap proxy
  Quality (4)  — earnings stability, gross profitability proxy
  Volume (3)   — volume trend, volume breakout, money flow
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# ═══════════════════════════════════════════════════════════════
# Value Factors (from fundamental PE/PB/PS data)
# ═══════════════════════════════════════════════════════════════

def value_ep(pe_ttm: float | None) -> float:
    """Earnings yield: 1/PE. Higher = cheaper."""
    if pe_ttm is None or np.isnan(pe_ttm) or pe_ttm <= 0:
        return np.nan
    return 1.0 / pe_ttm


def value_bp(pb_mrq: float | None) -> float:
    """Book-to-Price: 1/PB. Higher = cheaper."""
    if pb_mrq is None or np.isnan(pb_mrq) or pb_mrq <= 0:
        return np.nan
    return 1.0 / pb_mrq


def value_sp(ps_ttm: float | None) -> float:
    """Sales-to-Price: 1/PS. Higher = cheaper."""
    if ps_ttm is None or np.isnan(ps_ttm) or ps_ttm <= 0:
        return np.nan
    return 1.0 / ps_ttm


# ═══════════════════════════════════════════════════════════════
# Momentum Factors
# ═══════════════════════════════════════════════════════════════

def momentum_n(closes: np.ndarray, n: int = 21, skip: int = 1) -> float:
    """N-period momentum skipping recent days (to avoid reversal)."""
    if len(closes) < n + skip:
        return np.nan
    return float(closes[-skip-1] / closes[-n-skip-1] - 1.0)


def momentum_1m(closes: np.ndarray) -> float:
    """1-month (21d) momentum."""
    return momentum_n(closes, 21, 1)


def momentum_3m(closes: np.ndarray) -> float:
    """3-month (63d) momentum."""
    return momentum_n(closes, 63, 5)


def momentum_6m(closes: np.ndarray) -> float:
    """6-month (126d) momentum."""
    return momentum_n(closes, 126, 5)


def momentum_12m_1m(closes: np.ndarray) -> float:
    """12m-1m momentum (standard academic factor)."""
    if len(closes) < 252 + 21:
        return np.nan
    return float(closes[-21-1] / closes[-252-1] - 1.0)


def factor_rsi(closes: np.ndarray, period: int = 14) -> float:
    """RSI: -|RSI-50|, mean-reversion signal."""
    if len(closes) < period + 1: return np.nan
    d = np.diff(closes[-period-1:])
    g = np.clip(d, 0, None).mean()
    l = -np.clip(d, None, 0).mean()
    if l < 1e-12: return np.nan
    return -abs(50.0 - 100.0 / (1.0 + g / l))


def factor_macd_hist(closes: np.ndarray) -> float:
    """MACD histogram (DIF - DEA)."""
    if len(closes) < 35: return np.nan
    ema12 = _ema_val(closes, 12)
    ema26 = _ema_val(closes, 26)
    dif = ema12 - ema26
    dea = _ema_val_from_point(dif, closes, 12, 26, 9)
    return dif - dea


def factor_bollinger_pos(closes: np.ndarray) -> float:
    """Bollinger position: (close - lower) / (upper - lower)."""
    if len(closes) < 20: return np.nan
    m = closes[-20:].mean()
    s = closes[-20:].std(ddof=1)
    w = 4.0 * s  # bandwidth = 2σ * 2
    return (closes[-1] - (m - 2*s)) / w if w > 1e-12 else 0.5


def factor_ma_cross(closes: np.ndarray, fast: int = 20, slow: int = 60) -> float:
    """Moving average crossover: fast MA / slow MA - 1."""
    if len(closes) < slow: return np.nan
    fast_ma = closes[-fast:].mean()
    slow_ma = closes[-slow:].mean()
    if abs(slow_ma) < 1e-12: return 0.0
    return float(fast_ma / slow_ma - 1.0)


# ═══════════════════════════════════════════════════════════════
# Reversal Factors
# ═══════════════════════════════════════════════════════════════

def reversal_1d(closes: np.ndarray) -> float:
    """1-day reversal (negative yesterday's return)."""
    if len(closes) < 2: return np.nan
    return -float(closes[-1] / closes[-2] - 1.0)


def reversal_1w(closes: np.ndarray) -> float:
    """1-week reversal (negative 5-day return)."""
    if len(closes) < 6: return np.nan
    return -float(closes[-1] / closes[-6] - 1.0)


def reversal_1m(closes: np.ndarray) -> float:
    """1-month reversal (negative 21-day return)."""
    if len(closes) < 22: return np.nan
    return -float(closes[-1] / closes[-22] - 1.0)


# ═══════════════════════════════════════════════════════════════
# Volatility Factors
# ═══════════════════════════════════════════════════════════════

def realized_vol(closes: np.ndarray, window: int = 21) -> float:
    """Annualized realized volatility over window days."""
    if len(closes) < window + 1: return np.nan
    rets = np.diff(closes[-window-1:]) / closes[-window-1:-1]
    rets = rets[~np.isnan(rets)]
    if len(rets) < 10: return np.nan
    return float(np.std(rets, ddof=1) * np.sqrt(252))


def realized_vol_1m(closes: np.ndarray) -> float:
    return realized_vol(closes, 21)


def realized_vol_3m(closes: np.ndarray) -> float:
    return realized_vol(closes, 63)


def max_daily_return(closes: np.ndarray, window: int = 21) -> float:
    """Maximum daily return in window (lottery-demand proxy)."""
    if len(closes) < window + 1: return np.nan
    rets = np.diff(closes[-window-1:]) / closes[-window-1:-1]
    rets = rets[~np.isnan(rets)]
    if len(rets) == 0: return np.nan
    return -float(np.max(rets))  # negative: high max ret = bad


def return_skewness(closes: np.ndarray, window: int = 63) -> float:
    """Skewness of daily returns (investors prefer positive skew)."""
    if len(closes) < window + 3: return np.nan
    rets = np.diff(closes[-window-1:]) / closes[-window-1:-1]
    rets = rets[~np.isnan(rets)]
    if len(rets) < 20: return np.nan
    m = np.mean(rets)
    s = np.std(rets, ddof=1)
    if s < 1e-12: return 0.0
    return -float(np.mean((rets - m) ** 3) / (s ** 3))  # positive = investors like it


# ═══════════════════════════════════════════════════════════════
# Liquidity Factors
# ═══════════════════════════════════════════════════════════════

def turnover_avg(volumes: np.ndarray, window: int = 21) -> float:
    """Average turnover (log, higher = more liquid)."""
    if len(volumes) < window: return np.nan
    avg_v = volumes[-window:].mean()
    if avg_v < 1e-12: return np.nan
    return float(np.log(avg_v))


def dollar_volume(amounts: np.ndarray, window: int = 21) -> float:
    """Average dollar volume (log, higher = institutional)."""
    if len(amounts) < window: return np.nan
    avg_a = amounts[-window:].mean()
    if avg_a < 1e-12: return np.nan
    return float(np.log(avg_a))


def amihud_illiquidity(closes: np.ndarray, amounts: np.ndarray, window: int = 21) -> float:
    """Amihud illiquidity: avg(|ret| / dollar_volume). Higher = less liquid."""
    if len(closes) < window + 1: return np.nan
    rets = np.abs(np.diff(closes[-window-1:]) / closes[-window-1:-1])
    amounts_w = amounts[-window:]
    ratio = rets / np.maximum(amounts_w, 1e-12)
    ratio = ratio[~np.isnan(ratio)]
    if len(ratio) == 0: return np.nan
    return -float(np.mean(ratio))  # negative: higher illiquidity = worse


def volume_trend(volumes: np.ndarray, short: int = 5, long: int = 20) -> float:
    """Volume trend: short avg / long avg - 1."""
    if len(volumes) < long: return np.nan
    s_avg = volumes[-short:].mean()
    l_avg = volumes[-long:].mean()
    if l_avg < 1e-12: return 0.0
    return float(s_avg / l_avg - 1.0)


def volume_breakout(volumes: np.ndarray, window: int = 20) -> float:
    """Volume breakout: current vol / max(historical)."""
    if len(volumes) < window: return np.nan
    hist_max = volumes[-window:-1].max()
    if hist_max < 1e-12: return 1.0
    return float(volumes[-1] / hist_max)


# ═══════════════════════════════════════════════════════════════
# Quality Factors (price-derived proxies)
# ═══════════════════════════════════════════════════════════════

def earnings_stability(pe_values: np.ndarray) -> float:
    """Coefficient of variation of PE (proxy for earnings stability).
    Lower CV = more stable earnings = higher quality."""
    valid = pe_values[(~np.isnan(pe_values)) & (pe_values > 0)]
    if len(valid) < 10: return np.nan
    return -float(np.std(valid, ddof=1) / max(np.mean(valid), 1e-12))


def gross_profitability_proxy(closes: np.ndarray) -> float:
    """Proxy: negative of drawdown (less drawdown = more stable business)."""
    if len(closes) < 252: return np.nan
    peak = np.maximum.accumulate(closes[-252:])
    dd = (closes[-252:] - peak) / peak
    return -float(np.std(dd, ddof=1))  # lower DD volatility = higher quality


def price_momentum_quality(closes: np.ndarray) -> float:
    """Quality momentum: smooth positive returns over 12m."""
    if len(closes) < 252: return np.nan
    monthly_returns = []
    for i in range(12):
        start = -(i+1)*21 - 1
        end = -i*21 - 1
        if len(closes) > abs(start):
            monthly_returns.append(closes[end] / closes[start] - 1.0)
    if len(monthly_returns) < 6: return np.nan
    # Fraction of positive months
    pos_frac = np.mean([1 if r > 0 else 0 for r in monthly_returns])
    return float(pos_frac)


def leverage_proxy(closes: np.ndarray) -> float:
    """Proxy: maximum drawdown (higher DD = higher leverage risk)."""
    if len(closes) < 252: return np.nan
    peak = np.maximum.accumulate(closes[-252:])
    dd = (closes[-252:] - peak) / peak
    return -float(np.min(dd))  # negative: higher DD = worse


# ═══════════════════════════════════════════════════════════════
# Size Factor
# ═══════════════════════════════════════════════════════════════

def log_market_cap_proxy(amounts: np.ndarray, turnover: np.ndarray) -> float:
    """Log market cap proxy from amount (成交额) and turnover (换手率).
    circ_mv ≈ amount * 100 / turnover_rate."""
    if len(amounts) < 21 or len(turnover) < 21: return np.nan
    avg_amount = amounts[-21:].mean()
    avg_turnover = turnover[-21:].mean() if turnover[-21:].mean() > 0 else np.nan
    if np.isnan(avg_turnover) or avg_turnover < 1e-8:
        return np.nan
    circ_mv = avg_amount * 100.0 / avg_turnover
    if circ_mv <= 0: return np.nan
    return -float(np.log(circ_mv))  # negative: smaller = higher score (size premium)


# ═══════════════════════════════════════════════════════════════
# Money Flow Factor
# ═══════════════════════════════════════════════════════════════

def chaikin_money_flow(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                        volumes: np.ndarray, window: int = 20) -> float:
    """Chaikin Money Flow: measures accumulation/distribution."""
    if len(closes) < window + 1: return np.nan
    h, l, c, v = highs[-window-1:], lows[-window-1:], closes[-window-1:], volumes[-window-1:]
    hl_diff = np.where(h - l < 1e-12, 1e-12, h - l)
    mfm = ((c - l) - (h - c)) / hl_diff
    mfv = mfm * v
    total_vol = np.sum(v[-window:])
    if total_vol < 1e-12: return 0.0
    return float(np.sum(mfv[-window:]) / total_vol)


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _ema_val(series: np.ndarray, span: int) -> float:
    if len(series) < span: return np.nan
    alpha = 2.0 / (span + 1)
    r = series[:span].mean()
    for i in range(span, len(series)):
        r = alpha * series[i] + (1 - alpha) * r
    return float(r)


def _ema_val_from_point(dif: float, closes: np.ndarray, fast: int, slow: int, signal: int) -> float:
    """Compute signal-line EMA of DIF using historical closes as proxy."""
    if len(closes) < slow + signal: return dif
    dea = dif  # start with current DIF as initial DEA estimate
    alpha = 2.0 / (signal + 1)
    for i in range(len(closes) - signal, len(closes)):
        ema12 = _ema_val(closes[:i+1], fast) if i >= fast else closes[i]
        ema26 = _ema_val(closes[:i+1], slow) if i >= slow else closes[i]
        dif_i = ema12 - ema26
        dea = alpha * dif_i + (1 - alpha) * dea
    return float(dea)


# ═══════════════════════════════════════════════════════════════
# Factor Registry (all 31 factors)
# ═══════════════════════════════════════════════════════════════

@dataclass
class FactorSpec:
    """Factor specification with data requirements."""
    name: str
    func: callable
    data_cols: list[str]  # required data columns
    category: str  # value/momentum/reversal/volatility/liquidity/size/quality/volume
    higher_is_better: bool = True  # True if higher factor value = better stock


FACTOR_SPECS = [
    # Value
    FactorSpec("value_ep",     value_ep,       ["peTTM"],     "value"),
    FactorSpec("value_bp",     value_bp,       ["pbMRQ"],     "value"),
    FactorSpec("value_sp",     value_sp,       ["psTTM"],     "value"),
    # Momentum
    FactorSpec("momentum_1m",  momentum_1m,    ["close"],     "momentum"),
    FactorSpec("momentum_3m",  momentum_3m,    ["close"],     "momentum"),
    FactorSpec("momentum_6m",  momentum_6m,    ["close"],     "momentum"),
    FactorSpec("momentum_12m1m", momentum_12m_1m, ["close"], "momentum"),
    FactorSpec("rsi",          factor_rsi,     ["close"],     "momentum"),
    FactorSpec("macd_hist",    factor_macd_hist, ["close"],   "momentum"),
    FactorSpec("bollinger_pos", factor_bollinger_pos, ["close"], "momentum"),
    FactorSpec("ma_cross",     factor_ma_cross, ["close"],    "momentum"),
    # Reversal
    FactorSpec("reversal_1d",  reversal_1d,    ["close"],     "reversal"),
    FactorSpec("reversal_1w",  reversal_1w,    ["close"],     "reversal"),
    FactorSpec("reversal_1m",  reversal_1m,    ["close"],     "reversal"),
    # Volatility
    FactorSpec("vol_1m",       realized_vol_1m, ["close"],    "volatility"),
    FactorSpec("vol_3m",       realized_vol_3m, ["close"],    "volatility"),
    FactorSpec("max_daily_ret", max_daily_return, ["close"],  "volatility"),
    FactorSpec("ret_skew",     return_skewness, ["close"],    "volatility"),
    # Liquidity
    FactorSpec("turnover_avg", turnover_avg,   ["volume"],    "liquidity"),
    FactorSpec("dollar_vol",   dollar_volume,  ["amount"],    "liquidity"),
    FactorSpec("amihud_illiq", amihud_illiquidity, ["close", "amount"], "liquidity"),
    FactorSpec("vol_trend",    volume_trend,   ["volume"],    "liquidity"),
    FactorSpec("vol_breakout", volume_breakout, ["volume"],   "liquidity"),
    # Size
    FactorSpec("size_proxy",   log_market_cap_proxy, ["amount", "turnover"], "size"),
    # Quality
    FactorSpec("earn_stability", earnings_stability, ["peTTM"], "quality"),
    FactorSpec("gross_profit",  gross_profitability_proxy, ["close"], "quality"),
    FactorSpec("quality_mom",   price_momentum_quality, ["close"], "quality"),
    FactorSpec("leverage",      leverage_proxy, ["close"], "quality"),
    # Volume/Money Flow
    FactorSpec("money_flow",   chaikin_money_flow, ["high", "low", "close", "volume"], "volume"),
]

# Build lookup dicts
FACTOR_BY_NAME = {f.name: f for f in FACTOR_SPECS}
FACTOR_NAMES = [f.name for f in FACTOR_SPECS]
CATEGORIES = sorted(set(f.category for f in FACTOR_SPECS))

# Default weights (equal per category, equal within category)
_FACTORS_PER_CAT = {}
for f in FACTOR_SPECS:
    _FACTORS_PER_CAT[f.category] = _FACTORS_PER_CAT.get(f.category, 0) + 1

_cat_weight = 1.0 / len(CATEGORIES)
DEFAULT_WEIGHTS = {}
for f in FACTOR_SPECS:
    DEFAULT_WEIGHTS[f.name] = _cat_weight / _FACTORS_PER_CAT[f.category]

# Category weights for factor group weighting
CATEGORY_WEIGHTS = {cat: _cat_weight for cat in CATEGORIES}


def compute_factor(spec: FactorSpec, data: dict) -> float:
    """Compute a single factor from stock data dict."""
    try:
        args = [np.asarray(data[col]) if col in data and not isinstance(data[col], (int, float))
                else data.get(col) for col in spec.data_cols]
        return spec.func(*args)
    except (ValueError, TypeError, KeyError):
        return np.nan


def compute_all_factors(stock_data: dict, factor_names: list[str] = None) -> dict[str, float]:
    """Compute all specified factors for one stock."""
    names = factor_names or FACTOR_NAMES
    return {name: compute_factor(FACTOR_BY_NAME[name], stock_data) for name in names
            if name in FACTOR_BY_NAME}
