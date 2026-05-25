"""Tests for parameter change impact assessment."""
import numpy as np
from quant_trading.strategy.impact import (
    ChangeReport,
    ImpactMetrics,
    ParameterChange,
    _assess_risk,
    _compute_impact_metrics,
    evaluate_impact,
)


class TestImpactMetrics:
    def test_compute(self):
        before = np.random.RandomState(42).normal(0.001, 0.01, 250)
        after = np.random.RandomState(42).normal(0.0005, 0.015, 250)  # lower mean, higher vol
        metrics = _compute_impact_metrics(before, after)
        assert metrics.sharpe_after < metrics.sharpe_before

    def test_change_pct(self):
        m = ImpactMetrics(sharpe_before=1.0, sharpe_after=0.7, mdd_before=-0.2, mdd_after=-0.3,
                          win_rate_before=0.5, win_rate_after=0.4)
        assert m.sharpe_change_pct == -30.0

class TestRiskAssessment:
    def test_critical_on_large_degradation(self):
        m = ImpactMetrics(sharpe_before=2.0, sharpe_after=1.0, mdd_before=-0.1, mdd_after=-0.4,
                          win_rate_before=0.6, win_rate_after=0.3)
        risk = _assess_risk(m, [ParameterChange("window", 10, 100)])
        assert risk.risk_level == "critical"

    def test_low_risk_on_minor_change(self):
        m = ImpactMetrics(sharpe_before=1.0, sharpe_after=0.95, mdd_before=-0.2, mdd_after=-0.21,
                          win_rate_before=0.5, win_rate_after=0.49)
        risk = _assess_risk(m, [ParameterChange("threshold", 0.01, 0.012)])
        assert risk.risk_level in ("low", "medium")

    def test_approved_on_low(self):
        m = ImpactMetrics(sharpe_before=1.0, sharpe_after=0.98, mdd_before=-0.1, mdd_after=-0.1,
                          win_rate_before=0.5, win_rate_after=0.5)
        risk = _assess_risk(m, [ParameterChange("w", 5, 6)])
        assert risk.approved is True

class TestChangeReport:
    def test_full_report(self):
        before = np.random.RandomState(42).normal(0.001, 0.01, 100)
        after = np.random.RandomState(43).normal(0.0005, 0.012, 100)
        changes = [ParameterChange("window", 10, 20)]
        report = evaluate_impact(before, after, changes, "test_strategy")
        assert isinstance(report, ChangeReport)
        assert report.strategy_name == "test_strategy"

class TestParameterChange:
    def test_change_pct(self):
        pc = ParameterChange("x", 10, 15)
        assert pc.change_pct == 50.0
