"""Tests for counterfactual backtesting & stress tests (backtest-003)."""

import numpy as np
import pytest
from quant_trading.backtest.counterfactual import (
    CounterfactualScenario,
    HistoricalScenarios,
    ParameterPerturbation,
    ScenarioType,
    StressTestEngine,
    StressTestReport,
    StressTestResult,
)


class TestCounterfactualScenario:
    def test_shock_period(self):
        returns = np.zeros(100)
        scenario = CounterfactualScenario(
            name="test",
            scenario_type=ScenarioType.SHOCK_PERIOD,
            shock_value=-0.05,
            shock_start_idx=10,
            shock_duration=5,
        )
        modified = scenario.apply(returns)
        # First 10 unchanged, next 5 shocked, rest unchanged
        assert modified[0] == 0.0
        assert modified[10] == pytest.approx(-0.05)
        assert modified[14] == pytest.approx(-0.05)
        assert modified[15] == 0.0

    def test_shock_duration_clamped(self):
        returns = np.zeros(10)
        scenario = CounterfactualScenario(
            name="test",
            scenario_type=ScenarioType.SHOCK_PERIOD,
            shock_value=-0.05,
            shock_start_idx=8,
            shock_duration=10,
        )
        modified = scenario.apply(returns)
        # Should only shock indices 8-9 (2 remaining)
        assert modified[8] == pytest.approx(-0.05)
        assert modified[9] == pytest.approx(-0.05)

    def test_volatility_spike(self):
        rng = np.random.RandomState(42)
        returns = rng.normal(0, 0.01, 100)
        scenario = CounterfactualScenario(
            name="test",
            scenario_type=ScenarioType.VOLATILITY_SPIKE,
            vol_multiplier=3.0,
        )
        modified = scenario.apply(returns)
        assert np.std(modified) > np.std(returns) * 2

    def test_remove_outliers(self):
        returns = np.array([0.0] * 50 + [0.20, -0.25] + [0.0] * 48)
        scenario = CounterfactualScenario(
            name="test",
            scenario_type=ScenarioType.REMOVE_OUTLIERS,
            outlier_threshold=3.0,
        )
        modified = scenario.apply(returns)
        assert np.std(modified) < np.std(returns)

    def test_bootstrap(self):
        rng = np.random.RandomState(42)
        returns = rng.normal(0, 0.01, 50)
        scenario = CounterfactualScenario(
            name="test",
            scenario_type=ScenarioType.BOOTSTRAP,
        )
        modified = scenario.apply(returns)
        assert len(modified) == len(returns)
        # Bootstrap should produce different values
        assert not np.array_equal(modified, returns)

    def test_liquidity_crunch_reduces_volume(self):
        returns = np.zeros(20)
        volumes = np.ones(20) * 1000.0
        scenario = CounterfactualScenario(
            name="test",
            scenario_type=ScenarioType.LIQUIDITY_CRUNCH,
            volume_reduction=0.5,
        )
        scenario.apply(returns, volumes)
        assert volumes[0] == pytest.approx(500.0)

    def test_original_not_mutated(self):
        returns = np.ones(10) * 0.01
        original = returns.copy()
        scenario = CounterfactualScenario(
            name="test",
            scenario_type=ScenarioType.VOLATILITY_SPIKE,
            vol_multiplier=2.0,
        )
        scenario.apply(returns)
        assert np.array_equal(returns, original)  # Original unchanged


class TestHistoricalScenarios:
    def test_covid_crash(self):
        s = HistoricalScenarios.covid_crash_2020()
        assert s.name == "COVID-19 Crash 2020.03"
        assert s.shock_duration == 23
        assert s.vol_multiplier == 3.0

    def test_china_crash(self):
        s = HistoricalScenarios.china_crash_2015()
        assert "China" in s.name
        assert s.shock_value < -0.01

    def test_bear_market(self):
        s = HistoricalScenarios.bear_market_2022()
        assert s.shock_duration == 60

    def test_flash_crash(self):
        s = HistoricalScenarios.flash_crash()
        assert s.scenario_type == ScenarioType.LIQUIDITY_CRUNCH
        assert s.volume_reduction == 0.1

    def test_stagflation(self):
        s = HistoricalScenarios.stagflation()
        assert s.shock_duration == 120

    def test_all_scenarios(self):
        all_s = HistoricalScenarios.all_scenarios()
        assert len(all_s) == 5
        names = {s.name for s in all_s}
        assert "COVID-19 Crash 2020.03" in names


class TestStressTestEngine:
    @pytest.fixture
    def engine(self):
        return StressTestEngine()

    @pytest.fixture
    def sample_returns(self):
        rng = np.random.RandomState(42)
        return rng.normal(0.0005, 0.015, 252)  # ~8% annual, 24% vol

    def test_baseline_computation(self, engine, sample_returns):
        result = engine.compute_baseline(sample_returns)
        assert isinstance(result, StressTestResult)
        assert result.scenario_name == "Baseline"
        assert result.total_return is not None
        assert result.sharpe_ratio is not None
        assert result.max_drawdown is not None

    def test_run_scenario(self, engine, sample_returns):
        scenario = HistoricalScenarios.covid_crash_2020()
        result = engine.run_scenario(sample_returns, scenario)
        assert result.scenario_name == "COVID-19 Crash 2020.03"
        # Stress scenario should have non-zero metrics
        assert result.max_drawdown is not None
        assert result.sharpe_ratio is not None

    def test_run_all(self, engine, sample_returns):
        report = engine.run_all(sample_returns)
        assert isinstance(report, StressTestReport)
        assert len(report.scenarios) == 5
        assert report.baseline.scenario_name == "Baseline"
        assert report.worst_case is not None

    def test_report_summary(self, engine, sample_returns):
        report = engine.run_all(sample_returns)
        summary = report.summary()
        assert "baseline_return" in summary
        assert "scenarios_tested" in summary
        assert summary["scenarios_tested"] == 5

    def test_passed_flag_on_bad_scenario(self, engine):
        # Returns with -50% drawdown should fail
        bad_returns = np.array([-0.10] * 252)  # -10% daily → huge DD
        result = engine.compute_baseline(bad_returns)
        assert result.passed is False

    def test_var_computation(self, engine, sample_returns):
        result = engine.compute_baseline(sample_returns)
        assert result.var_95 < 0  # VaR should be negative (loss)
        assert result.cvar_95 <= result.var_95  # CVaR ≤ VaR

    def test_liquidity_metrics(self, engine, sample_returns):
        volumes = np.random.RandomState(42).lognormal(10, 1, 252)
        result = engine.compute_baseline(sample_returns, volumes)
        assert 0 <= result.liquidity_gap_pct <= 1

    def test_max_drawdown_across_all(self, engine):
        report = StressTestReport(
            baseline=StressTestResult(
                scenario_name="Base",
                total_return=0.1,
                annualized_return=0.1,
                annualized_volatility=0.15,
                sharpe_ratio=0.67,
                max_drawdown=-0.1,
                max_drawdown_days=10,
                max_leverage=2.0,
                var_95=-0.02,
                cvar_95=-0.03,
                liquidity_gap_pct=0.1,
                worst_day_return=-0.03,
                vs_baseline_return=0.0,
                passed=True,
            ),
            scenarios=[
                StressTestResult(
                    scenario_name="Bad",
                    total_return=-0.3,
                    annualized_return=-0.3,
                    annualized_volatility=0.3,
                    sharpe_ratio=-1.0,
                    max_drawdown=-0.5,
                    max_drawdown_days=30,
                    max_leverage=5.0,
                    var_95=-0.08,
                    cvar_95=-0.10,
                    liquidity_gap_pct=0.5,
                    worst_day_return=-0.10,
                    vs_baseline_return=-0.4,
                    passed=False,
                ),
            ],
        )
        assert report.max_drawdown_across_all == pytest.approx(-0.5)
        assert report.all_passed is False


class TestParameterPerturbation:
    @pytest.fixture
    def base_returns(self):
        rng = np.random.RandomState(42)
        return rng.normal(0.001, 0.01, 252)

    def test_slippage(self, base_returns):
        result = ParameterPerturbation.test_slippage(base_returns)
        assert result.parameter == "slippage_bps"
        assert len(result.test_values) == 6
        assert len(result.sharpe_values) == 6
        # Higher slippage → lower sharpe
        assert result.sharpe_values[-1] <= result.sharpe_values[0]

    def test_costs(self, base_returns):
        result = ParameterPerturbation.test_costs(base_returns)
        assert result.parameter == "slippage_bps"  # Same engine, renamed

    def test_latency(self, base_returns):
        result = ParameterPerturbation.test_latency(base_returns)
        assert result.parameter == "latency_ms"
        assert len(result.sharpe_values) == 6

    def test_sensitivity_nonzero(self, base_returns):
        result = ParameterPerturbation.test_slippage(base_returns, [0, 50])
        assert result.sensitivity > 0  # Non-zero sensitivity expected
