
"""Algorithmic trading strategies (exec-003). AGENTS.md §7: VWAP/TWAP/POV."""
import numpy as np


def vwap_schedule(total_qty: float, periods: int) -> np.ndarray:
    weights = np.ones(periods) / periods
    return weights * total_qty
