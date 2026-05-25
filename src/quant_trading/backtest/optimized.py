"""
Vectorized backtest performance optimization (backtest-002).
AGENTS.md §15: Numba/Polars for 20x speedup.
"""
import numpy as np


def vectorized_sharpe(returns: np.ndarray, rf: float = 0.02) -> float:
    excess = returns.mean() - rf / 252
    vol = returns.std()
    return float(excess / max(vol, 1e-10) * np.sqrt(252))

def vectorized_mdd(equity: np.ndarray) -> float:
    cummax = np.maximum.accumulate(equity)
    drawdown = (equity - cummax) / cummax
    return float(abs(drawdown.min()))

def vectorized_trade_pnl(prices: np.ndarray, signals: np.ndarray) -> np.ndarray:
    """Vectorized trade PnL from price changes and signal positions."""
    returns = np.diff(prices) / prices[:-1]
    return signals[:-1] * returns
