"""
Portfolio optimization (portfolio-001).
AGENTS.md §26: Risk parity, Sharpe-based weighting, rebalancing.
"""
import numpy as np


class PortfolioOptimizer:
    @staticmethod
    def equal_weight(n_assets: int) -> np.ndarray:
        return np.ones(n_assets) / n_assets

    @staticmethod
    def risk_parity(cov_matrix: np.ndarray, max_iter: int = 100) -> np.ndarray:
        n = len(cov_matrix)
        w = np.ones(n) / n
        for _ in range(max_iter):
            sigma = np.sqrt(w @ cov_matrix @ w)
            mrc = cov_matrix @ w / sigma
            risk_budget = w * mrc
            if np.allclose(risk_budget, risk_budget.mean(), atol=1e-6): break
            w = w * risk_budget.mean() / (mrc + 1e-10)
            w = w / w.sum()
        return w

    @staticmethod
    def max_sharpe(returns: np.ndarray, rf: float = 0.02) -> np.ndarray:
        mean = returns.mean(axis=0) - rf/252
        cov = np.cov(returns.T)
        try:
            inv_cov = np.linalg.inv(cov)
            w = inv_cov @ mean
            return w / w.sum()
        except np.linalg.LinAlgError:
            return PortfolioOptimizer.equal_weight(returns.shape[1])
