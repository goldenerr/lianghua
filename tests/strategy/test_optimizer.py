"""Tests for strategy optimizer — grid search + walk-forward."""
import numpy as np
from quant_trading.strategy.optimizer import (
    grid_search, walk_forward_optimize, evaluate_params,
    Objective, TrialResult, OptimizationResult,
)


class TestEvaluateParams:
    def test_basic(self):
        rng = np.random.RandomState(42)
        returns = rng.normal(0.001, 0.02, 252)
        result = evaluate_params({"fast": 10, "slow": 30}, returns)
        assert isinstance(result, TrialResult)
        assert result.sharpe != 0
        assert result.total_return != 0
        assert result.max_drawdown <= 0

    def test_insufficient_data(self):
        result = evaluate_params({"p": 1}, np.array([0.01]))
        assert result.sharpe == 0.0
        assert result.params == {"p": 1}

    def test_min_mdd_objective(self):
        rng = np.random.RandomState(42)
        returns = rng.normal(0.001, 0.02, 252)
        result = evaluate_params({"fast": 5}, returns, Objective.MIN_MDD)
        assert result.max_drawdown <= 0

    def test_all_positive(self):
        returns = np.array([0.01] * 20)
        result = evaluate_params({"p": 1}, returns)
        assert result.win_rate == 1.0
        # All identical returns = zero vol = Sharpe 0, which is correct
        assert result.sharpe == 0.0


class TestGridSearch:
    def test_simple_grid(self):
        rng = np.random.RandomState(42)
        returns = rng.normal(0.001, 0.02, 252)
        param_grid = {"fast": [5, 10], "slow": [20, 30]}
        result = grid_search(param_grid, returns)
        assert isinstance(result, OptimizationResult)
        assert len(result.all_trials) == 4  # 2x2
        assert "fast" in result.best_params

    def test_empty_grid(self):
        result = grid_search({}, np.array([0.01, -0.01, 0.02]))
        assert result.best_params == {}

    def test_max_sharpe(self):
        rng = np.random.RandomState(42)
        returns = rng.normal(0.001, 0.02, 252)
        param_grid = {"window": [5, 10, 20]}
        result = grid_search(param_grid, returns, Objective.MAX_SHARPE)
        assert result.objective == Objective.MAX_SHARPE

    def test_min_mdd(self):
        rng = np.random.RandomState(42)
        returns = rng.normal(0.001, 0.02, 252)
        param_grid = {"window": [5, 20]}
        result = grid_search(param_grid, returns, Objective.MIN_MDD)
        assert result.objective == Objective.MIN_MDD

    def test_max_calmar(self):
        rng = np.random.RandomState(42)
        returns = rng.normal(0.001, 0.01, 252)
        param_grid = {"w": [10, 20]}
        result = grid_search(param_grid, returns, Objective.MAX_CALMAR)
        assert result.objective == Objective.MAX_CALMAR

    def test_summary(self):
        rng = np.random.RandomState(42)
        returns = rng.normal(0.001, 0.02, 252)
        result = grid_search({"w": [5, 10]}, returns)
        summary = result.summary()
        assert "best_params" in summary
        assert "best_score" in summary
        assert "trials" in summary


class TestWalkForward:
    def test_basic(self):
        rng = np.random.RandomState(42)
        returns = rng.normal(0.001, 0.02, 500)
        param_grid = {"fast": [5, 10], "slow": [20, 25]}
        result = walk_forward_optimize(param_grid, returns, n_folds=3)
        assert isinstance(result, OptimizationResult)
        assert result.in_sample_score != 0

    def test_small_data_fallback(self):
        """Small data should reduce n_folds automatically."""
        rng = np.random.RandomState(42)
        returns = rng.normal(0.001, 0.02, 50)
        result = walk_forward_optimize({"w": [5, 10]}, returns, n_folds=5)
        assert isinstance(result, OptimizationResult)

    def test_in_sample_vs_out_of_sample(self):
        rng = np.random.RandomState(42)
        returns = rng.normal(0.001, 0.02, 500)
        result = walk_forward_optimize({"w": [5, 10]}, returns, n_folds=3)
        assert result.in_sample_score != 0
        assert result.out_of_sample_score != 0
