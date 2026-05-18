"""Tests for risk engine."""
import numpy as np
from quant_trading.risk.engine import RiskEngine, RiskLimits, RiskCheckResult

class TestRiskEngine:
    def test_position_limit_ok(self):
        engine = RiskEngine()
        result = engine.check_position_limit(10_000, 100_000)
        assert result.passed is True

    def test_position_limit_breach(self):
        engine = RiskEngine(RiskLimits(max_position_pct=0.10))
        result = engine.check_position_limit(20_000, 100_000)
        assert result.passed is False

    def test_leverage_ok(self):
        engine = RiskEngine()
        result = engine.check_leverage(100_000, 100_000)
        assert result.passed is True

    def test_leverage_breach(self):
        engine = RiskEngine(RiskLimits(max_leverage=1.5))
        result = engine.check_leverage(200_000, 100_000)
        assert result.passed is False

    def test_daily_loss_breach(self):
        engine = RiskEngine()
        result = engine.check_daily_loss(-5000, 100_000)
        assert result.passed is False  # 5% > 2%

    def test_var_check(self):
        engine = RiskEngine()
        returns = np.random.RandomState(42).normal(0, 0.01, 252)
        result = engine.check_var(returns)
        assert bool(result.passed) is True

    def test_mdd_liquidate(self):
        engine = RiskEngine()
        result = engine.check_mdd(0.30)
        assert result.passed is False
        assert "LIQUIDATE" in result.reason
