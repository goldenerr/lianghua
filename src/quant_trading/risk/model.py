"""
V6 Risk Model — Barra-style multi-factor risk decomposition.
Industry-standard risk analytics for institutional portfolios.

STYLE FACTORS: Value, Momentum, Volatility, Size, Liquidity, Quality, Growth
COVARIANCE: Ledoit-Wolf shrinkage with EWMA decay
SPECIFIC RISK: Bayesian shrinkage estimator
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import numpy as np
from scipy.linalg import sqrtm


# ═══════════════════════════════════════════════════════════════
# Risk Model Configuration
# ═══════════════════════════════════════════════════════════════

@dataclass
class RiskModelConfig:
    """Risk model calibration parameters."""
    # Factor covariance
    cov_method: str = "ledoit_wolf"   # ledoit_wolf, ewma, sample
    ewma_lambda: float = 0.94         # EWMA decay (RiskMetrics)
    shrinkage_target: str = "constant_correlation"  # or "diagonal"
    
    # Specific risk
    specific_risk_method: str = "bayesian"  # bayesian, structural, sample
    specific_risk_shrinkage: float = 0.3    # shrinkage towards structural
    
    # VaR / CVaR
    var_confidence: float = 0.95
    cvar_confidence: float = 0.99
    horizon_days: int = 1
    
    # Newey-West (auto-correlation adjustment)
    newey_west_lags: int = 5


# ═══════════════════════════════════════════════════════════════
# Factor Covariance Estimation
# ═══════════════════════════════════════════════════════════════

def estimate_factor_covariance(
    factor_returns: np.ndarray,  # (n_factors, n_periods)
    config: RiskModelConfig = None,
) -> np.ndarray:
    """Estimate factor covariance matrix with shrinkage."""
    if config is None:
        config = RiskModelConfig()
    
    n_factors, n_periods = factor_returns.shape
    
    if config.cov_method == "ewma":
        lambd = config.ewma_lambda
        weights = np.array([(1 - lambd) * lambd ** (n_periods - 1 - t)
                           for t in range(n_periods)])
        weights /= weights.sum()
        centered = factor_returns - np.average(factor_returns, axis=1, weights=weights).reshape(-1, 1)
        cov = centered @ np.diag(weights) @ centered.T
    elif config.cov_method == "ledoit_wolf":
        sample_cov = np.cov(factor_returns, ddof=1)
        # Constant correlation target
        vols = np.sqrt(np.diag(sample_cov))
        corr = sample_cov / np.outer(vols, np.maximum(vols, 1e-12))
        np.fill_diagonal(corr, 0)
        mean_corr = np.sum(corr) / (n_factors * (n_factors - 1)) if n_factors > 1 else 0
        target_corr = mean_corr * np.ones((n_factors, n_factors))
        np.fill_diagonal(target_corr, 1)
        target = np.outer(vols, vols) * target_corr
        delta = _compute_shrinkage(factor_returns, sample_cov, target)
        cov = delta * target + (1 - delta) * sample_cov
    else:
        cov = np.cov(factor_returns, ddof=1)
    
    # Newey-West adjustment for auto-correlation
    cov = _newey_west_adjust(factor_returns, cov, config.newey_west_lags)
    
    # Ensure positive semi-definite
    eigvals, eigvecs = np.linalg.eigh(cov)
    eigvals = np.maximum(eigvals, 1e-8)
    cov = eigvecs @ np.diag(eigvals) @ eigvecs.T
    
    return cov


def _compute_shrinkage(returns, sample_cov, target):
    """Ledoit-Wolf optimal shrinkage intensity."""
    n, T = returns.shape
    rets_centered = returns - returns.mean(axis=1, keepdims=True)
    pi_mat = np.zeros((n, n))
    for t in range(T):
        x = rets_centered[:, t]
        pi_mat += np.outer(x, x) ** 2
    pi_hat = np.sum(pi_mat - sample_cov ** 2) / T
    gamma_hat = np.sum((sample_cov - target) ** 2)
    delta = max(0, min(1, pi_hat / max(gamma_hat, 1e-12) / T))
    return delta


def _newey_west_adjust(returns, cov, max_lags):
    """Newey-West auto-correlation consistent covariance adjustment."""
    n_factors, n_periods = returns.shape
    if n_periods < 3 or max_lags < 1:
        return cov
    
    rets_centered = returns - returns.mean(axis=1, keepdims=True)
    adjustment = np.zeros_like(cov)
    
    for lag in range(1, max_lags + 1):
        weight = 1 - lag / (max_lags + 1)
        for t in range(n_periods - lag):
            adjustment += weight * (np.outer(rets_centered[:, t], rets_centered[:, t+lag]) +
                                    np.outer(rets_centered[:, t+lag], rets_centered[:, t]))
    
    return cov + adjustment / n_periods


# ═══════════════════════════════════════════════════════════════
# Specific Risk Estimation
# ═══════════════════════════════════════════════════════════════

def estimate_specific_risk(
    asset_returns: np.ndarray,      # (n_assets, n_periods)
    factor_exposures: np.ndarray,   # (n_assets, n_factors)
    factor_returns: np.ndarray,     # (n_factors, n_periods)
    config: RiskModelConfig = None,
) -> np.ndarray:
    """Estimate specific (idiosyncratic) risk for each asset.
    
    Uses Bayesian shrinkage: blend sample residual vol with structural estimate
    (inverse of sqrt(market cap) proxy).
    """
    if config is None:
        config = RiskModelConfig()
    
    n_assets, n_periods = asset_returns.shape
    
    # Residual returns: r_i - Σ X_ik * f_k
    residuals = asset_returns - factor_exposures @ factor_returns
    
    # Sample specific variance
    sample_var = np.var(residuals, ddof=1, axis=1)
    sample_vol = np.sqrt(np.maximum(sample_var, 1e-12))
    
    if config.specific_risk_method == "bayesian":
        # Structural estimate: median vol across all assets
        structural_vol = np.median(sample_vol)
        
        # Bayesian shrinkage: blend sample towards structural
        # Shrinkage weight based on estimation error
        shrinkage = config.specific_risk_shrinkage
        blended_var = (1 - shrinkage) * sample_var + shrinkage * (structural_vol ** 2)
        return np.sqrt(np.maximum(blended_var, 1e-12))
    else:
        return sample_vol


# ═══════════════════════════════════════════════════════════════
# Portfolio Risk Decomposition (Barra-style)
# ═══════════════════════════════════════════════════════════════

@dataclass
class PortfolioRisk:
    """Full portfolio risk decomposition."""
    total_risk: float             # annualized %
    systematic_risk: float        # from factor exposures
    specific_risk: float          # idiosyncratic
    factor_contributions: dict[str, float]  # risk contribution per factor
    var_95: float                 # Value at Risk (95%)
    cvar_95: float                # Conditional VaR (95%)
    tracking_error: float = 0.0   # vs benchmark


def decompose_portfolio_risk(
    weights: np.ndarray,           # (n_assets,)
    factor_exposures: np.ndarray,  # (n_assets, n_factors)
    factor_cov: np.ndarray,        # (n_factors, n_factors)
    specific_risk: np.ndarray,     # (n_assets,)
    factor_names: list[str] = None,
    benchmark_weights: np.ndarray = None,
    horizon_days: int = 1,
) -> PortfolioRisk:
    """Barra-style portfolio risk decomposition."""
    
    # Systematic risk: sqrt(w' X Σ_f X' w)
    systematic_var = weights @ factor_exposures @ factor_cov @ factor_exposures.T @ weights
    systematic_risk = np.sqrt(max(systematic_var, 0))
    
    # Specific risk: sqrt(w' diag(σ²_s) w)
    specific_var = np.sum((weights * specific_risk) ** 2)
    specific_risk_val = np.sqrt(max(specific_var, 0))
    
    # Total risk
    total_var = systematic_var + specific_var
    total_risk = np.sqrt(max(total_var, 0))
    
    # Annualize
    ann_factor = np.sqrt(252 / horizon_days)
    systematic_risk *= ann_factor
    specific_risk_val *= ann_factor
    total_risk *= ann_factor
    
    # Factor contributions
    factor_contribs = {}
    if factor_names:
        fx = factor_exposures
        f_contrib = weights @ fx * (fx.T @ weights @ factor_cov)
        for i, name in enumerate(factor_names):
            factor_contribs[name] = float(f_contrib[i])
    
    # VaR / CVaR (parametric, assuming normality)
    from scipy.stats import norm
    z_95 = norm.ppf(0.95)
    z_99 = norm.ppf(0.99)
    daily_total_risk = total_risk / ann_factor
    
    var_95 = z_95 * daily_total_risk  # negative in loss terms
    # CVaR for normal: φ(z) / (1-α) * σ
    cvar_95 = norm.pdf(z_95) / (1 - 0.95) * daily_total_risk
    
    # Tracking error vs benchmark
    te = 0.0
    if benchmark_weights is not None:
        active_weights = weights - benchmark_weights
        active_sys = active_weights @ factor_exposures @ factor_cov @ factor_exposures.T @ active_weights
        active_spec = np.sum((active_weights * specific_risk) ** 2)
        te = np.sqrt(max(active_sys + active_spec, 0)) * ann_factor
    
    return PortfolioRisk(
        total_risk=float(total_risk),
        systematic_risk=float(systematic_risk),
        specific_risk=float(specific_risk_val),
        factor_contributions=factor_contribs,
        var_95=float(var_95),
        cvar_95=float(cvar_95),
        tracking_error=float(te),
    )


# ═══════════════════════════════════════════════════════════════
# Stress Testing
# ═══════════════════════════════════════════════════════════════

STRESS_SCENARIOS = {
    "2020_covid_crash": {
        "value": -0.08, "momentum": -0.05, "volatility": 0.15,
        "size": -0.03, "liquidity": -0.10,
    },
    "2015_summer_crash": {
        "value": -0.02, "momentum": -0.08, "volatility": 0.20,
        "size": -0.05, "liquidity": -0.12,
    },
    "2008_gfc": {
        "value": -0.05, "momentum": -0.10, "volatility": 0.25,
        "size": -0.08, "liquidity": -0.15,
    },
    "rate_hike_shock": {
        "value": 0.03, "momentum": -0.03, "volatility": 0.05,
    },
    "liquidity_crisis": {
        "liquidity": -0.20, "size": -0.10,
    },
}


def stress_test_portfolio(
    weights: np.ndarray,
    factor_exposures: np.ndarray,
    factor_names: list[str],
    benchmark_weights: np.ndarray = None,
) -> dict[str, float]:
    """Stress test portfolio under historical and hypothetical scenarios."""
    results = {}
    base_exposures = factor_exposures.T @ weights
    if benchmark_weights is not None:
        base_exposures -= factor_exposures.T @ benchmark_weights
    
    for scenario_name, factor_shocks in STRESS_SCENARIOS.items():
        pnl = 0.0
        for factor, shock in factor_shocks.items():
            if factor in factor_names:
                idx = factor_names.index(factor)
                pnl += base_exposures[idx] * shock
        results[scenario_name] = float(pnl)
    
    return results
