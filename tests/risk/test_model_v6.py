import numpy as np
from quant_trading.risk.model import (
    RiskModelConfig,
    decompose_portfolio_risk,
    estimate_factor_covariance,
    estimate_specific_risk,
    stress_test_portfolio,
)


def _risk_inputs() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(42)
    factor_returns = rng.normal(0.0, 0.01, size=(3, 160))
    exposures = np.array([[1.0, 0.2, -0.1], [0.5, -0.3, 0.4], [-0.2, 0.8, 0.3]])
    residuals = rng.normal(0.0, 0.004, size=(3, 160))
    asset_returns = exposures @ factor_returns + residuals
    return factor_returns, exposures, asset_returns


def test_factor_covariance_methods_produce_positive_semidefinite_matrices() -> None:
    factor_returns, _, _ = _risk_inputs()
    for method in ("sample", "ewma", "ledoit_wolf"):
        cov = estimate_factor_covariance(factor_returns, RiskModelConfig(cov_method=method))
        assert cov.shape == (3, 3)
        assert np.linalg.eigvalsh(cov).min() >= -1e-12


def test_barra_decomposition_and_stress_report_portfolio_risk() -> None:
    factor_returns, exposures, asset_returns = _risk_inputs()
    factor_names = ["value", "momentum", "volatility"]
    cov = estimate_factor_covariance(factor_returns)
    specific = estimate_specific_risk(asset_returns, exposures, factor_returns)
    weights = np.array([0.4, 0.35, 0.25])
    benchmark = np.array([1 / 3, 1 / 3, 1 / 3])

    result = decompose_portfolio_risk(
        weights,
        exposures,
        cov,
        specific,
        factor_names=factor_names,
        benchmark_weights=benchmark,
    )
    assert result.total_risk > 0
    assert result.cvar_95 > result.var_95 > 0
    assert result.tracking_error > 0
    assert set(result.factor_contributions) == set(factor_names)

    stresses = stress_test_portfolio(weights, exposures, factor_names, benchmark)
    assert "2020_covid_crash" in stresses
    assert all(np.isfinite(value) for value in stresses.values())
