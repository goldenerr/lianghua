
"""Counterfactual backtesting (backtest-003). AGENTS.md §5: parameter perturbation, custom scenarios."""
import numpy as np
def perturb_returns(returns: np.ndarray, shock: float = -0.10) -> np.ndarray:
    return returns + shock / len(returns)
