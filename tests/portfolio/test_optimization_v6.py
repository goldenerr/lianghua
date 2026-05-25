import numpy as np
import pytest
from quant_trading.portfolio.optimization import (
    black_litterman,
    diversification_ratio,
    effective_n,
    efficient_frontier,
    estimate_covariance,
    max_sharpe_portfolio,
    min_variance_portfolio,
    portfolio_risk_decomposition,
    risk_parity,
    risk_parity_with_constraints,
)


def _portfolio_inputs() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(17)
    returns = rng.normal(0.0005, 0.01, size=(5, 180))
    cov = estimate_covariance(returns, method="ledoit_wolf")
    expected = np.array([0.09, 0.07, 0.12, 0.08, 0.06])
    return returns, cov, expected


def test_covariance_and_risk_parity_portfolios_are_feasible() -> None:
    returns, cov, _ = _portfolio_inputs()
    for method in ("sample", "ewma", "ledoit_wolf"):
        assert estimate_covariance(returns, method=method).shape == (5, 5)

    unconstrained = risk_parity(cov)
    constrained = risk_parity_with_constraints(cov, max_weight=0.30)
    assert np.isclose(unconstrained.sum(), 1.0)
    assert np.isclose(constrained.sum(), 1.0)
    assert constrained.max() <= 0.30 + 1e-6


def test_mvo_black_litterman_and_analytics_produce_actionable_outputs() -> None:
    _, cov, expected = _portfolio_inputs()
    max_sharpe = max_sharpe_portfolio(expected, cov, max_weight=0.35)
    min_var = min_variance_portfolio(cov, max_weight=0.35)
    frontier = efficient_frontier(cov, expected, n_points=5, max_weight=0.40)
    posterior = black_litterman(
        np.full(5, 0.20),
        cov,
        views={2: 0.14},
        view_confidences={2: 0.75},
    )

    assert np.isclose(max_sharpe.sum(), 1.0)
    assert np.isclose(min_var.sum(), 1.0)
    assert frontier
    assert posterior.shape == (5,)
    assert np.isfinite(posterior).all()

    analytics = portfolio_risk_decomposition(min_var, cov)
    assert analytics["total_risk"] > 0
    assert np.isclose(analytics["risk_contrib_pct"].sum(), 1.0)
    assert diversification_ratio(min_var, cov) >= 1.0
    assert effective_n(np.full(5, 0.20)) == pytest.approx(5.0)


def test_optimization_rejects_infeasible_position_limits() -> None:
    _, cov, expected = _portfolio_inputs()
    with pytest.raises(ValueError, match="infeasible"):
        min_variance_portfolio(cov, max_weight=0.10)
    with pytest.raises(ValueError, match="infeasible"):
        max_sharpe_portfolio(expected, cov, max_weight=0.10)
