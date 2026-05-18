"""Tests for smoke test framework — exercises full system pipeline."""
import numpy as np
import pandas as pd
from quant_trading.smoke_test import (
    run_full_smoke_test, SmokeTestResult, SimpleBacktestEngine,
    sma_crossover,
)
from quant_trading.backtest.engine import BacktestConfig, BacktestResult
from quant_trading.test_harness import smoke_test, monte_carlo_test


class TestSMAStrategy:
    def test_long_signal(self):
        prices = np.concatenate([np.ones(30) * 90, np.ones(10) * 110])
        assert sma_crossover(prices, fast=5, slow=20) == 1

    def test_short_signal(self):
        prices = np.concatenate([np.ones(30) * 110, np.ones(10) * 90])
        assert sma_crossover(prices, fast=5, slow=20) == 0

    def test_insufficient_data(self):
        assert sma_crossover(np.ones(5), fast=5, slow=30) == 0


class TestSimpleBacktestEngine:
    def test_run_with_random_data(self):
        rng = np.random.RandomState(42)
        prices = 100 * np.cumprod(1 + rng.normal(0.0005, 0.015, 200))
        data = {"TEST": pd.DataFrame({"close": prices}, index=pd.date_range("2024-01-01", periods=200))}
        engine = SimpleBacktestEngine()

        def strat(p):
            return sma_crossover(p, 10, 30)

        result = engine.run(data, strat)
        assert isinstance(result, BacktestResult)
        assert result.metrics.total_return != 0
        assert result.metrics.sharpe_ratio != 0

    def test_deterministic(self):
        rng = np.random.RandomState(99)
        prices = 100 * np.cumprod(1 + rng.normal(0.0005, 0.015, 200))
        data = {"TEST": pd.DataFrame({"close": prices})}
        engine = SimpleBacktestEngine()
        config = BacktestConfig(deterministic=True)

        def strat(p):
            return sma_crossover(p, 10, 30)

        r1 = engine.run(data, strat, config)
        r2 = engine.run(data, strat, config)
        assert r1.metrics.sharpe_ratio == r2.metrics.sharpe_ratio

    def test_forward_bias_check(self):
        rng = np.random.RandomState(42)
        prices = 100 * np.cumprod(1 + rng.normal(0.0005, 0.015, 200))
        idx = pd.date_range("2024-01-01", periods=200)
        df = pd.DataFrame({"close": prices}, index=idx)
        # Nearly constant signal → autocorrelation ~1.0 (catches suspicious patterns)
        df["signal"] = np.linspace(0.01, 0.02, 200) + rng.normal(0, 0.0001, 200)

        engine = SimpleBacktestEngine()
        violations = engine.validate_no_forward_bias(df, signal_col="signal")
        assert len(violations) > 0

    def test_clean_data_no_bias(self):
        rng = np.random.RandomState(42)
        df = pd.DataFrame({
            "close": 100 * np.cumprod(1 + rng.normal(0.0005, 0.015, 200)),
        }, index=pd.date_range("2024-01-01", periods=200))
        df["signal"] = np.random.choice([-1, 0, 1], size=200)  # random signals

        engine = SimpleBacktestEngine()
        violations = engine.validate_no_forward_bias(df, signal_col="signal")
        assert len(violations) == 0


class TestFullSmokeTest:
    def test_run(self):
        result = run_full_smoke_test(seed=42)
        assert isinstance(result, SmokeTestResult)
        assert result.passed
        assert len(result.steps) >= 7
        assert result.duration_seconds > 0

    def test_all_steps_named(self):
        result = run_full_smoke_test(seed=123)
        step_names = {s["step"] for s in result.steps}
        expected = {"order_manager", "kill_switch", "backtest_engine",
                     "report_generation", "forward_bias_check", "monitor_health",
                     "backtest_consistency", "config_defaults"}
        assert step_names == expected

    def test_every_step_passed(self):
        result = run_full_smoke_test(seed=456)
        for step in result.steps:
            assert step["passed"], f"Step {step['step']} failed: {step.get('detail')}"

    def test_reproducible(self):
        r1 = run_full_smoke_test(seed=42)
        r2 = run_full_smoke_test(seed=42)
        for s1, s2 in zip(r1.steps, r2.steps):
            assert s1["passed"] == s2["passed"]


class TestHarnessStubs:
    def test_smoke_test_stub(self):
        result = smoke_test(["backtest", "risk", "execution"])
        assert result["all_importable"] is True
        assert len(result["modules"]) == 3

    def test_monte_carlo_test(self):
        rng = np.random.RandomState(42)

        def always_pass(rng_state):
            return True

        result = monte_carlo_test(always_pass, n_iter=100, seed=42)
        assert result["pass_rate"] == 1.0
        assert result["iterations"] == 100
