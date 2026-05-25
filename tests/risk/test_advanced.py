"""Tests for dynamic stress generator, model sentinel, and drift report."""
import numpy as np
from quant_trading.risk.dynamic_stress import (
    MarketRegime,
    detect_regime,
    generate_regime_scenarios,
)
from quant_trading.risk.model_risk import DriftReport
from quant_trading.risk.model_sentinel import ModelHealth, ModelSentinel


class TestMarketRegime:
    def test_normal_regime(self):
        mr = MarketRegime(volatility=0.01, correlation=0.5, liquidity=0.8)
        assert not mr.is_stressed
        assert mr.regime_label == "normal"

    def test_stressed_vol(self):
        mr = MarketRegime(volatility=0.04, correlation=0.5, liquidity=0.8)
        assert mr.is_stressed

    def test_stressed_liquidity(self):
        mr = MarketRegime(volatility=0.01, correlation=0.5, liquidity=0.2)
        assert mr.is_stressed


class TestDetectRegime:
    def test_normal_returns(self):
        rng = np.random.RandomState(42)
        returns = rng.normal(0.001, 0.01, 252)  # daily returns
        regime = detect_regime(returns)
        assert regime.volatility > 0
        assert regime.regime_label in ("normal", "high_vol", "crisis")

    def test_high_vol_returns(self):
        rng = np.random.RandomState(42)
        returns = rng.normal(0, 0.03, 252)
        regime = detect_regime(returns)
        assert regime.volatility > 0.2

    def test_with_volumes(self):
        rng = np.random.RandomState(42)
        returns = rng.normal(0.001, 0.01, 252)
        volumes = rng.uniform(1000, 2000, 252)
        regime = detect_regime(returns, volumes)
        assert regime.liquidity > 0

    def test_multi_asset_correlation(self):
        rng = np.random.RandomState(42)
        base = rng.normal(0, 0.01, 252)
        returns = np.column_stack([
            base,
            base * 0.8 + rng.normal(0, 0.002, 252),
            -base * 0.7 + rng.normal(0, 0.002, 252),
        ])
        regime = detect_regime(returns)
        assert regime.correlation > 0.6

    def test_label_crisis(self):
        rng = np.random.RandomState(42)
        returns = rng.normal(-0.005, 0.04, 252)  # very high vol
        regime = detect_regime(returns)
        assert regime.volatility > 0.4
        assert regime.regime_label == "crisis"


class TestGenerateRegimeScenarios:
    def test_generates_correct_count(self):
        rng = np.random.RandomState(42)
        returns = rng.normal(0.001, 0.01, 252)
        regime = detect_regime(returns)
        scenarios = generate_regime_scenarios(regime, n_scenarios=3, n_days=20)
        assert len(scenarios) == 3
        for s in scenarios:
            assert len(s) == 20

    def test_scenarios_increase_shock(self):
        rng = np.random.RandomState(42)
        returns = rng.normal(0.001, 0.01, 252)
        regime = detect_regime(returns)
        scenarios = generate_regime_scenarios(regime, n_scenarios=5, n_days=10)
        # first scenario should have lower vol than last
        assert abs(scenarios[0]).mean() < abs(scenarios[-1]).mean()

    def test_scenarios_are_deterministic_by_default(self):
        regime = MarketRegime(volatility=0.2, correlation=0.7, liquidity=0.4)
        a = generate_regime_scenarios(regime, n_scenarios=2, n_days=5)
        b = generate_regime_scenarios(regime, n_scenarios=2, n_days=5)
        assert np.allclose(a[0], b[0])


class TestModelSentinel:
    def test_healthy(self):
        ms = ModelSentinel(psi_threshold=0.2)
        preds = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        actuals = np.array([1.1, 2.1, 3.1, 4.1, 5.1])
        assert ms.check(preds, actuals) == ModelHealth.HEALTHY

    def test_critical(self):
        ms = ModelSentinel(psi_threshold=0.01)
        preds = np.array([1.0, 2.0, 3.0])
        actuals = np.array([10.0, 20.0, 30.0])
        assert ms.check(preds, actuals) == ModelHealth.CRITICAL

    def test_fallback(self):
        ms = ModelSentinel()
        assert "rule_based" in ms.fallback()

    def test_perfect_prediction(self):
        ms = ModelSentinel(psi_threshold=0.01)
        preds = np.array([1.0, 2.0, 3.0])
        assert ms.check(preds, preds) == ModelHealth.HEALTHY


class TestDriftReport:
    def test_defaults(self):
        dr = DriftReport()
        assert dr.psi == 0.0
        assert dr.alert is False

    def test_with_data(self):
        dr = DriftReport(psi=0.3, feature_drift={"f1": 0.1}, alert=True)
        assert dr.psi == 0.3
        assert dr.alert is True
