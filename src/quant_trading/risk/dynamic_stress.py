"""
Dynamic stress test scenario generator (risk-004).
AGENTS.md: 基于当前市场 regime 自动生成压力场景，每天凌晨自动运行。
"""
import numpy as np
from dataclasses import dataclass
from datetime import datetime, timezone

@dataclass
class MarketRegime:
    volatility: float; correlation: float; liquidity: float
    regime_label: str = "normal"
    @property
    def is_stressed(self) -> bool: return self.volatility > 0.03 or self.liquidity < 0.3

def detect_regime(returns: np.ndarray, volumes: np.ndarray = None) -> MarketRegime:
    vol = float(np.std(returns, ddof=1) * np.sqrt(252))
    corr = 0.5  # placeholder for multi-asset
    liq = 1.0 if volumes is None else float(np.median(volumes) / (np.median(volumes) + np.std(volumes)))
    label = "crisis" if vol > 0.4 else "high_vol" if vol > 0.25 else "normal"
    return MarketRegime(volatility=round(vol, 4), correlation=round(corr, 2), liquidity=round(liq, 2), regime_label=label)

def generate_regime_scenarios(regime: MarketRegime, n_scenarios: int = 3, n_days: int = 20) -> list[np.ndarray]:
    rng = np.random.RandomState(int(datetime.now(timezone.utc).timestamp()) % 10000)
    scenarios = []
    for i in range(n_scenarios):
        shock = regime.volatility * (1.0 + i * 0.5)
        scenarios.append(rng.normal(-shock / 2, shock, n_days))
    return scenarios
