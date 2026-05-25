"""Tests for walk-forward, Monte Carlo, and stress testing."""
import numpy as np
import pandas as pd
import pytest
from quant_trading.backtest.engine import (
    BacktestConfig,
    BacktestEngine,
    BacktestResult,
    MetricsCalculator,
)
from quant_trading.backtest.monte_carlo import MonteCarloSimulator, StressTestRunner
from quant_trading.backtest.walk_forward import WalkForwardValidator

# ── Mock engine ───────────────────────────────────────────────────────────────

def _mock_strategy(data, params=None):
    """Simple mock strategy that generates random signals."""
    pass

class MockEngine(BacktestEngine):
    def __init__(self):
        super().__init__("mock")

    def run(self, data, strategy, config=None):
        cfg = config or BacktestConfig()
        rng = np.random.RandomState(42 if cfg.deterministic else None)
        n_days = 252
        returns = rng.normal(0.0005, 0.01, n_days)
        equity = pd.Series(
            100_000 * (1 + returns).cumprod(),
            index=pd.bdate_range("2024-01-01", periods=n_days, freq="B"),
        )
        trades = pd.DataFrame({
            "pnl": rng.normal(500, 2000, 100),
            "hold_days": rng.randint(1, 20, 100),
        })
        trades.loc[rng.choice(100, 30, replace=False), "pnl"] *= -1
        metrics = MetricsCalculator.from_equity_curve(equity, trades)
        return BacktestResult(metrics=metrics, equity_curve=equity, trades=trades, passed=True)


# ── WalkForwardValidator ──────────────────────────────────────────────────────

class TestWalkForwardValidator:
    def test_runs_folds(self):
        engine = MockEngine()
        config = BacktestConfig(wf_folds=3, wf_oos_pct=0.2)
        wf = WalkForwardValidator(engine, config, purge_days=5)

        data = {"TEST": pd.DataFrame(
            {"close": np.random.randn(200) + 100},
            index=pd.DatetimeIndex(pd.bdate_range("2024-01-01", periods=200, freq="B")),
        )}
        result = wf.run(data, _mock_strategy)
        assert result.wf_folds is not None
        assert result.wf_oos_metrics is not None

    def test_empty_data(self):
        engine = MockEngine()
        wf = WalkForwardValidator(engine, BacktestConfig(), purge_days=5)
        result = wf.run({}, _mock_strategy)
        assert len(result.errors) > 0

    def test_sensitivity_test(self):
        engine = MockEngine()
        config = BacktestConfig(deterministic=True)
        wf = WalkForwardValidator(engine, config, purge_days=5)

        data = {"TEST": pd.DataFrame(
            {"close": np.random.randn(300) + 100},
            index=pd.DatetimeIndex(pd.bdate_range("2024-01-01", periods=300, freq="B")),
        )}
        result = wf.sensitivity_test(
            data,
            lambda params: _mock_strategy,
            {"window": 20},
            "window",
            perturbation_pct=0.10,
        )
        assert "sensitivity" in result
        assert "passed" in result


# ── MonteCarloSimulator ───────────────────────────────────────────────────────

class TestMonteCarloSimulator:
    def test_runs_simulations(self):
        config = BacktestConfig(mc_runs=100, deterministic=True)
        sim = MonteCarloSimulator(config)
        trades = pd.DataFrame({
            "pnl": np.random.RandomState(42).normal(500, 2000, 80),
        })
        result = sim.run(trades)
        assert "sharpe" in result
        assert result["n_simulations"] == 100
        assert "mean" in result["sharpe"]

    def test_empty_trades(self):
        config = BacktestConfig(mc_runs=10)
        sim = MonteCarloSimulator(config)
        result = sim.run(pd.DataFrame())
        assert "error" in result

    def test_mdd_distribution(self):
        config = BacktestConfig(mc_runs=50, deterministic=True)
        sim = MonteCarloSimulator(config)
        trades = pd.DataFrame({
            "pnl": np.random.RandomState(42).normal(200, 1500, 60),
        })
        result = sim.run(trades)
        assert "mdd" in result
        assert float(result["mdd"]["mean"]) >= 0


# ── StressTestRunner ──────────────────────────────────────────────────────────

class TestStressTestRunner:
    def test_apply_covid_scenario(self):
        eq = pd.Series(
            100_000 * (1 + np.random.RandomState(42).normal(0.0005, 0.01, 252)).cumprod(),
            index=pd.bdate_range("2024-01-01", periods=252, freq="B"),
        )
        metrics = StressTestRunner.apply_scenario(eq, "covid_2020")
        assert metrics.max_drawdown > 0
        assert metrics.sharpe_ratio is not None

    def test_apply_all_scenarios(self):
        eq = pd.Series(
            100_000 * (1 + np.random.RandomState(42).normal(0.0005, 0.01, 252)).cumprod(),
            index=pd.bdate_range("2024-01-01", periods=252, freq="B"),
        )
        results = StressTestRunner.run_all_scenarios(eq)
        assert "covid_2020" in results
        assert "china_2015" in results
        assert "bear_2022" in results
        for name, r in results.items():
            assert "sharpe" in r
            assert "mdd_pct" in r

    def test_unknown_scenario_raises(self):
        eq = pd.Series([100, 101, 102])
        with pytest.raises(KeyError):
            StressTestRunner.apply_scenario(eq, "mars_crash")
