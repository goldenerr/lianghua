"""Tests for backtest engine, metrics, and report."""
import numpy as np
import pandas as pd
import pytest
from quant_trading.backtest.engine import (
    BacktestConfig,
    BacktestRegistry,
    BacktestResult,
    MetricsCalculator,
    PerformanceMetrics,
)
from quant_trading.backtest.report import ReportGenerator


def _make_equity_curve(n=252, trend=0.0005, vol=0.01, seed=42):
    rng = np.random.RandomState(seed)
    returns = rng.normal(trend, vol, n)
    equity = 100000 * (1 + returns).cumprod()
    return pd.Series(equity, index=pd.bdate_range("2024-01-01", periods=n, freq="B"))


def _make_trades(n=50):
    rng = np.random.RandomState(42)
    df = pd.DataFrame({
        "pnl": rng.normal(500, 2000, n),
        "hold_days": rng.randint(1, 30, n),
    })
    df.loc[rng.choice(n, int(n*0.3), replace=False), "pnl"] *= -1  # 30% losers
    return df


class TestMetricsCalculator:
    def test_basic_metrics(self):
        eq = _make_equity_curve()
        metrics = MetricsCalculator.from_equity_curve(eq)
        assert metrics.total_return is not None
        assert metrics.sharpe_ratio != 0

    def test_sharpe_positive_trend(self):
        eq = _make_equity_curve(trend=0.001, vol=0.005)
        metrics = MetricsCalculator.from_equity_curve(eq)
        assert metrics.sharpe_ratio > 0

    def test_mdd_non_negative(self):
        eq = _make_equity_curve()
        metrics = MetricsCalculator.from_equity_curve(eq)
        assert metrics.max_drawdown >= 0

    def test_trade_metrics(self):
        eq = _make_equity_curve()
        trades = _make_trades()
        metrics = MetricsCalculator.from_equity_curve(eq, trades)
        assert metrics.total_trades == 50
        assert 0 <= metrics.win_rate <= 1
        assert metrics.profit_factor > 0

    def test_empty_equity(self):
        metrics = MetricsCalculator.from_equity_curve(pd.Series(dtype=float))
        assert metrics.total_return == 0

    def test_bankrupt_equity_caps_annualized_loss(self):
        equity = pd.Series([100.0, 50.0, -10.0], index=pd.date_range("2024-01-01", periods=3))
        metrics = MetricsCalculator.from_equity_curve(equity)
        assert metrics.total_return < -1.0
        assert metrics.annualized_return == -1.0
        assert metrics.total_trades == 0

    def test_deterministic_results(self):
        eq1 = _make_equity_curve(seed=42)
        eq2 = _make_equity_curve(seed=42)
        m1 = MetricsCalculator.from_equity_curve(eq1)
        m2 = MetricsCalculator.from_equity_curve(eq2)
        assert m1.sharpe_ratio == pytest.approx(m2.sharpe_ratio)


class TestBacktestResult:
    def test_default_metrics(self):
        result = BacktestResult()
        assert result.passed is False
        assert result.metrics.total_return == 0

    def test_check_gates_sharpe_fails(self):
        config = BacktestConfig(min_sharpe=2.0)
        result = BacktestResult()
        result.metrics = PerformanceMetrics(sharpe_ratio=1.0, win_rate=0.5, max_drawdown=0.1)
        result.check_gates(config)
        assert result.passed is False

    def test_check_gates_all_pass(self):
        config = BacktestConfig(min_sharpe=1.0, max_mdd=0.20, min_win_rate=0.30)
        result = BacktestResult()
        result.metrics = PerformanceMetrics(
            sharpe_ratio=1.5, win_rate=0.45, max_drawdown=0.10
        )
        result.check_gates(config)
        assert result.passed is True


class TestBacktestRegistry:
    def test_register_and_get(self):
        from quant_trading.backtest.engine import BacktestEngine
        class DummyEngine(BacktestEngine):
            def __init__(self):
                super().__init__("dummy")
            def run(self, data, strategy, config=None):
                return BacktestResult()
        BacktestRegistry.register("dummy", DummyEngine)
        engine = BacktestRegistry.create("dummy")
        assert engine.name == "dummy"

    def test_list_engines(self):
        engines = BacktestRegistry.list_engines()
        assert "dummy" in engines

    def test_get_unknown_raises(self):
        with pytest.raises(KeyError):
            BacktestRegistry.get("nonexistent")


class TestReportGenerator:
    def test_generate_report(self):
        eq = _make_equity_curve(n=100)
        metrics = MetricsCalculator.from_equity_curve(eq)
        result = BacktestResult(metrics=metrics, passed=True)
        config = BacktestConfig()
        result.config = config
        result.check_gates(config)

        report = ReportGenerator.generate(result)
        assert "performance_summary" in report
        assert "quality_gates" in report
        ps = report["performance_summary"]
        assert "sharpe_ratio" in ps

    def test_summary_string(self):
        eq = _make_equity_curve(n=100)
        metrics = MetricsCalculator.from_equity_curve(eq)
        result = BacktestResult(metrics=metrics, passed=True)
        config = BacktestConfig()
        result.config = config

        report = ReportGenerator.generate(result)
        summary = ReportGenerator.summary(report)
        assert "SHARPE" in summary.upper()
        assert "MAX DRAWDOWN" in summary.upper()

    def test_json_serializable(self):
        import json
        result = BacktestResult(
            metrics=MetricsCalculator.from_equity_curve(_make_equity_curve(n=50)),
            passed=True,
        )
        result.config = BacktestConfig()
        report = ReportGenerator.generate(result)
        json_str = ReportGenerator.to_json(report)
        parsed = json.loads(json_str)
        assert "performance_summary" in parsed
