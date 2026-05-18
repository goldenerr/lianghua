"""
Order book reconstruction & algorithmic trading (exec-003).
AGENTS.md §7: VWAP/TWAP/POV 拆分算法.
"""
import numpy as np

def vwap_schedule(total_qty: float, periods: int, volume_profile: np.ndarray = None) -> np.ndarray:
    weights = volume_profile / volume_profile.sum() if volume_profile is not None else np.ones(periods) / periods
    return np.round(weights * total_qty, 2)

def twap_schedule(total_qty: float, periods: int) -> np.ndarray:
    return np.full(periods, round(total_qty / periods, 2))

def pov_schedule(total_qty: float, participation_rate: float, expected_volume: float) -> float:
    """Return slice size for POV (Percentage of Volume)."""
    return min(total_qty, expected_volume * participation_rate)

class OrderBookSnapshot:
    def __init__(self, bids: list[tuple[float, float]], asks: list[tuple[float, float]]):
        self.bids = sorted(bids, key=lambda x: -x[0]); self.asks = sorted(asks, key=lambda x: x[0])
    def vwap(self, side: str, qty: float) -> float:
        levels = self.asks if side == "buy" else self.bids
        remaining, cost = qty, 0.0
        for price, vol in levels:
            fill = min(remaining, vol); cost += fill * price; remaining -= fill
            if remaining <= 0: break
        return round(cost / (qty - remaining), 4) if remaining < qty else levels[-1][0]
