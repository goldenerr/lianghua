"""
Dynamic slippage & market impact model (exec-002).
AGENTS.md §7: 回测必须使用动态滑点（基于订单簿深度或波动率）.
"""

from dataclasses import dataclass

import numpy as np


@dataclass
class SlippageModel:
    base_bps: float = 1.0
    vol_sensitivity: float = 0.5
    size_sensitivity: float = 0.1

    def estimate(self, volatility: float, order_size_pct: float) -> float:
        """Estimate slippage in basis points. vol as daily std, size as % of daily volume."""
        bps = (
            self.base_bps
            + self.vol_sensitivity * volatility * 10000
            + self.size_sensitivity * order_size_pct * 100
        )
        return max(0.0, round(bps, 2))


@dataclass
class ImpactModel:
    """Square-root market impact model: I = σ * sqrt(Q / ADV) * κ."""

    kappa: float = 0.1

    def estimate(self, volatility: float, order_shares: float, adv: float) -> float:
        if adv <= 0:
            return 0.0
        return float(round(self.kappa * volatility * np.sqrt(order_shares / adv), 6))
