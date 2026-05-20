"""
V6 Portfolio Optimization — Risk Parity, MVO, Black-Litterman.
Industry-standard portfolio construction beyond equal-weight.

References:
  - Roncalli (2013) "Introduction to Risk Parity and Budgeting"
  - Markowitz (1952) "Portfolio Selection"
  - Black & Litterman (1992) "Global Portfolio Optimization"
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Callable
import numpy as np
from scipy.optimize import minimize


# ═══════════════════════════════════════════════════════════════
# Covariance Estimation
# ═══════════════════════════════════════════════════════════════

def estimate_covariance(returns: np.ndarray, method: str = "ledoit_wolf") -> np.ndarray:
    """Estimate covariance matrix from asset returns.
    
    Args:
        returns: (n_assets, n_periods) array of returns
        method: 'sample', 'ledoit_wolf' (shrinkage), 'ewma' (decay=0.94)
    """
    n_assets, n_periods = returns.shape
    
    if method == "ledoit_wolf":
        # Ledoit-Wolf shrinkage: optimal blend of sample cov and identity
        sample_cov = np.cov(returns, ddof=1)
        
        # Compute shrinkage target (constant correlation)
        vols = np.sqrt(np.diag(sample_cov))
        corr = sample_cov / np.outer(vols, vols)
        np.fill_diagonal(corr, 0)
        mean_corr = np.sum(corr) / (n_assets * (n_assets - 1)) if n_assets > 1 else 0
        target_corr = np.ones((n_assets, n_assets)) * mean_corr
        np.fill_diagonal(target_corr, 1)
        target = np.outer(vols, vols) * target_corr
        
        # Optimal shrinkage intensity
        delta = _optimal_shrinkage_intensity(returns, sample_cov, target)
        shrunk = delta * target + (1 - delta) * sample_cov
        
        return shrunk
    
    elif method == "ewma":
        # EWMA with lambda=0.94 (RiskMetrics standard)
        lambd = 0.94
        weights = np.array([(1 - lambd) * lambd ** (n_periods - 1 - t) 
                           for t in range(n_periods)])
        weights /= weights.sum()
        weighted_rets = returns - np.average(returns, axis=1, weights=weights).reshape(-1, 1)
        return weighted_rets @ np.diag(weights) @ weighted_rets.T
    
    else:
        return np.cov(returns, ddof=1)


def _optimal_shrinkage_intensity(returns, sample_cov, target):
    """Compute optimal Ledoit-Wolf shrinkage intensity."""
    n, T = returns.shape
    rets_centered = returns - returns.mean(axis=1, keepdims=True)
    
    # Asymptotic variance of sample covariance
    pi_mat = np.zeros((n, n))
    for t in range(T):
        x = rets_centered[:, t]
        pi_mat += np.outer(x, x) ** 2  # element-wise square
    pi_hat = np.sum(pi_mat - sample_cov ** 2) / T
    
    # Distance to target
    gamma_hat = np.sum((sample_cov - target) ** 2)
    
    delta = max(0, min(1, pi_hat / max(gamma_hat, 1e-12) / T))
    return delta


# ═══════════════════════════════════════════════════════════════
# Risk Parity
# ═══════════════════════════════════════════════════════════════

def risk_parity(cov_matrix: np.ndarray, max_iter: int = 100, tol: float = 1e-8) -> np.ndarray:
    """Compute risk parity (equal risk contribution) portfolio weights.
    
    Minimizes: Σ_i Σ_j (w_i·(Σw)_i - w_j·(Σw)_j)²
    subject to: w_i ≥ 0, Σw_i = 1
    """
    n = len(cov_matrix)
    w0 = np.ones(n) / n
    
    def risk_budget_objective(w):
        portfolio_vol = np.sqrt(w @ cov_matrix @ w)
        if portfolio_vol < 1e-12: return 0.0
        marginal_contrib = cov_matrix @ w
        risk_contrib = w * marginal_contrib / portfolio_vol
        target_contrib = portfolio_vol / n
        return np.sum((risk_contrib - target_contrib) ** 2)
    
    constraints = [{'type': 'eq', 'fun': lambda w: np.sum(w) - 1}]
    bounds = [(0, 1) for _ in range(n)]
    
    result = minimize(risk_budget_objective, w0, method='SLSQP',
                      constraints=constraints, bounds=bounds,
                      options={'maxiter': max_iter, 'ftol': tol})
    
    w = result.x
    return w / w.sum()  # ensure sum=1


def risk_parity_with_constraints(cov_matrix: np.ndarray, max_weight: float = 0.20,
                                  min_weight: float = 0.0, max_iter: int = 100) -> np.ndarray:
    """Risk parity with per-asset weight constraints."""
    n = len(cov_matrix)
    w0 = np.ones(n) / n
    
    def objective(w):
        if np.any(w < min_weight) or np.any(w > max_weight):
            return 1e10
        portfolio_vol = np.sqrt(w @ cov_matrix @ w)
        if portfolio_vol < 1e-12: return 0.0
        marginal = cov_matrix @ w
        risk_contrib = w * marginal / portfolio_vol
        target = portfolio_vol / n
        return np.sum((risk_contrib - target) ** 2)
    
    constraints = [{'type': 'eq', 'fun': lambda w: np.sum(w) - 1}]
    result = minimize(objective, w0, method='SLSQP', constraints=constraints,
                      options={'maxiter': max_iter})
    return result.x


# ═══════════════════════════════════════════════════════════════
# Mean-Variance Optimization (MVO)
# ═══════════════════════════════════════════════════════════════

def max_sharpe_portfolio(expected_returns: np.ndarray, cov_matrix: np.ndarray,
                          risk_free: float = 0.025, max_weight: float = 0.20,
                          long_only: bool = True) -> np.ndarray:
    """Maximum Sharpe ratio portfolio with constraints.
    
    Maximizes: (w·μ - rf) / √(w'Σw)
    subject to: Σw=1, w_i ≤ max_weight, w_i ≥ 0 (if long_only)
    """
    n = len(expected_returns)
    
    def neg_sharpe(w):
        port_ret = w @ expected_returns
        port_vol = np.sqrt(max(w @ cov_matrix @ w, 1e-20))
        return -(port_ret - risk_free) / port_vol
    
    constraints = [{'type': 'eq', 'fun': lambda w: np.sum(w) - 1}]
    bounds = [(0, max_weight) for _ in range(n)] if long_only else [(-max_weight, max_weight) for _ in range(n)]
    w0 = np.ones(n) / n
    
    result = minimize(neg_sharpe, w0, method='SLSQP', constraints=constraints,
                      bounds=bounds, options={'maxiter': 200})
    return result.x / result.x.sum()


def min_variance_portfolio(cov_matrix: np.ndarray, max_weight: float = 0.20) -> np.ndarray:
    """Global minimum variance portfolio."""
    n = len(cov_matrix)
    
    def port_vol(w):
        return np.sqrt(max(w @ cov_matrix @ w, 1e-20))
    
    constraints = [{'type': 'eq', 'fun': lambda w: np.sum(w) - 1}]
    bounds = [(0, max_weight) for _ in range(n)]
    w0 = np.ones(n) / n
    
    result = minimize(port_vol, w0, method='SLSQP', constraints=constraints,
                      bounds=bounds, options={'maxiter': 200})
    return result.x / result.x.sum()


def efficient_frontier(cov_matrix: np.ndarray, expected_returns: np.ndarray,
                        n_points: int = 20, max_weight: float = 0.20) -> list[tuple[float, float, np.ndarray]]:
    """Compute efficient frontier points (return, vol, weights)."""
    n = len(expected_returns)
    min_ret = np.min(expected_returns)
    max_ret = np.max(expected_returns)
    target_returns = np.linspace(min_ret, max_ret, n_points)
    
    frontier = []
    for target_ret in target_returns:
        def port_vol(w): return np.sqrt(max(w @ cov_matrix @ w, 1e-20))
        constraints = [
            {'type': 'eq', 'fun': lambda w: np.sum(w) - 1},
            {'type': 'eq', 'fun': lambda w, t=target_ret: w @ expected_returns - t},
        ]
        bounds = [(0, max_weight) for _ in range(n)]
        w0 = np.ones(n) / n
        
        result = minimize(port_vol, w0, method='SLSQP', constraints=constraints,
                          bounds=bounds, options={'maxiter': 200})
        if result.success:
            w = result.x / result.x.sum()
            frontier.append((target_ret, port_vol(w), w))
    
    return frontier


# ═══════════════════════════════════════════════════════════════
# Black-Litterman Model
# ═══════════════════════════════════════════════════════════════

def black_litterman(market_weights: np.ndarray, cov_matrix: np.ndarray,
                     views: dict[int, float], view_confidences: dict[int, float],
                     risk_aversion: float = 2.5, tau: float = 0.05) -> np.ndarray:
    """Black-Litterman model for combining market equilibrium with investor views.
    
    Args:
        market_weights: benchmark/market cap weights (prior)
        cov_matrix: asset covariance matrix
        views: {asset_index: expected_excess_return} dict
        view_confidences: {asset_index: confidence (0-1)} dict
        risk_aversion: market risk aversion parameter
        tau: uncertainty scaling of prior
    
    Returns: BL posterior expected returns
    """
    n = len(market_weights)
    
    # Market equilibrium returns (implied by CAPM)
    pi = risk_aversion * cov_matrix @ market_weights
    
    # View matrix P and view returns Q
    k = len(views)
    P = np.zeros((k, n))
    Q = np.zeros(k)
    Omega_diag = np.zeros(k)
    
    for i, (asset_idx, view_return) in enumerate(views.items()):
        P[i, asset_idx] = 1.0
        Q[i] = view_return
        confidence = view_confidences.get(asset_idx, 0.5)
        # Higher confidence = lower Omega (view variance)
        Omega_diag[i] = (1.0 / max(confidence, 0.01) - 1.0) * (P[i] @ cov_matrix @ P[i])
    
    Omega = np.diag(Omega_diag)
    
    # Posterior expected returns: [τΣ⁻¹ + P'Ω⁻¹P]⁻¹ [τΣ⁻¹π + P'Ω⁻¹Q]
    tau_Sigma_inv = np.linalg.inv(tau * cov_matrix)
    Omega_inv = np.linalg.inv(Omega)
    
    posterior_cov = np.linalg.inv(tau_Sigma_inv + P.T @ Omega_inv @ P)
    posterior_mean = posterior_cov @ (tau_Sigma_inv @ pi + P.T @ Omega_inv @ Q)
    
    return posterior_mean


# ═══════════════════════════════════════════════════════════════
# Portfolio Analytics
# ═══════════════════════════════════════════════════════════════

def portfolio_risk_decomposition(weights: np.ndarray, cov_matrix: np.ndarray) -> dict:
    """Decompose portfolio risk into marginal contributions."""
    port_vol = np.sqrt(weights @ cov_matrix @ weights)
    if port_vol < 1e-12:
        return {"total_risk": 0.0, "marginal_contrib": np.zeros_like(weights),
                "risk_contrib_pct": np.zeros_like(weights)}
    
    marginal = cov_matrix @ weights
    risk_contrib = weights * marginal / port_vol
    
    return {
        "total_risk": float(port_vol),
        "marginal_contrib": marginal,
        "risk_contrib_pct": risk_contrib / port_vol,
    }


def diversification_ratio(weights: np.ndarray, cov_matrix: np.ndarray) -> float:
    """Diversification ratio: weighted avg vol / portfolio vol."""
    vols = np.sqrt(np.diag(cov_matrix))
    weighted_vol = weights @ vols
    port_vol = np.sqrt(weights @ cov_matrix @ weights)
    if port_vol < 1e-12: return 0.0
    return float(weighted_vol / port_vol)


def effective_n(weights: np.ndarray) -> float:
    """Effective number of assets (inverse Herfindahl)."""
    n = np.sum(weights ** 2)
    return 1.0 / n if n > 1e-12 else 0.0
