"""
Dynamic stress test scenario generator (risk-004).
AGENTS.md: 基于当前市场 regime 自动生成压力场景，每天凌晨自动运行。
"""
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np


@dataclass
class MarketRegime:
    volatility: float; correlation: float; liquidity: float
    regime_label: str = "normal"
    @property
    def is_stressed(self) -> bool: return self.volatility > 0.03 or self.liquidity < 0.3

def detect_regime(returns: np.ndarray, volumes: np.ndarray = None) -> MarketRegime:
    arr = np.asarray(returns, dtype=float)
    if arr.ndim == 1:
        vol = float(np.std(arr, ddof=1) * np.sqrt(252))
        corr = 1.0
    else:
        # Multi-asset regime: use average annualized asset volatility and
        # average absolute pairwise correlation as a compact stress signal.
        vol = float(np.nanmean(np.std(arr, axis=0, ddof=1)) * np.sqrt(252))
        corr_matrix = np.corrcoef(arr, rowvar=False)
        upper = corr_matrix[np.triu_indices_from(corr_matrix, k=1)]
        corr = float(np.nanmean(np.abs(upper))) if upper.size else 1.0
    liq = 1.0 if volumes is None else float(np.median(volumes) / (np.median(volumes) + np.std(volumes)))
    label = "crisis" if vol > 0.4 else "high_vol" if vol > 0.25 else "normal"
    return MarketRegime(volatility=round(vol, 4), correlation=round(corr, 2), liquidity=round(liq, 2), regime_label=label)

def generate_regime_scenarios(
    regime: MarketRegime,
    n_scenarios: int = 3,
    n_days: int = 20,
    seed: int | None = 42,
) -> list[np.ndarray]:
    rng = np.random.RandomState(seed if seed is not None else int(datetime.now(timezone.utc).timestamp()) % 10000)
    scenarios = []
    for i in range(n_scenarios):
        correlation_multiplier = 1.0 + max(regime.correlation, 0.0) * 0.25
        liquidity_multiplier = 1.0 + max(0.0, 0.5 - regime.liquidity)
        shock = regime.volatility * (1.0 + i * 0.5) * correlation_multiplier * liquidity_multiplier
        scenarios.append(rng.normal(-shock / 2, shock, n_days))
    return scenarios
