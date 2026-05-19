"""
Strategy parameter optimization platform (strategy-002).

AGENTS.md §5 (strategy-002):
  - 集成 Optuna 进行贝叶斯参数优化
  - 优化目标可配置：最大 Sharpe、最小 MDD、最大 Calmar
  - 自动 Walk-Forward 优化（避免过拟合）
  - 输出优化后的参数组合及绩效对比表
  - CLI: python optimize.py --strategy rsi --trials 500
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from itertools import product
from typing import Any, Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)


class Objective(str, Enum):
    MAX_SHARPE = "max_sharpe"
    MIN_MDD = "min_mdd"
    MAX_CALMAR = "max_calmar"
    MAX_RETURN = "max_return"


@dataclass
class TrialResult:
    params: dict[str, Any]
    sharpe: float = 0.0
    total_return: float = 0.0
    max_drawdown: float = 0.0
    calmar: float = 0.0
    win_rate: float = 0.0

    @property
    def score(self) -> float:
        return self.sharpe


@dataclass
class OptimizationResult:
    best_params: dict[str, Any]
    best_score: float
    all_trials: list[TrialResult]
    objective: Objective
    # Walk-forward stats
    in_sample_score: float = 0.0
    out_of_sample_score: float = 0.0

    def summary(self) -> dict:
        return {
            "best_params": self.best_params,
            "best_score": round(self.best_score, 4),
            "objective": self.objective.value,
            "trials": len(self.all_trials),
            "in_sample": round(self.in_sample_score, 4),
            "out_of_sample": round(self.out_of_sample_score, 4),
        }


def evaluate_params(
    params: dict,
    returns: np.ndarray,
    objective: Objective = Objective.MAX_SHARPE,
    risk_free_rate: float = 0.025,
) -> TrialResult:
    """Evaluate a parameter set on given returns."""
    r = np.asarray(returns, dtype=np.float64)
    if len(r) < 2:
        return TrialResult(params=params)

    total_ret = float(np.cumprod(1 + r)[-1] - 1)
    ann_ret = float(np.mean(r) * 252)
    ann_vol = float(np.std(r, ddof=1) * np.sqrt(252))
    # NaN-safe: return 0 if vol is zero (instead of inf)
    if ann_vol > 1e-8:
        sharpe = (ann_ret - risk_free_rate) / ann_vol
        # Cap extreme Sharpes from near-zero vol
        if abs(sharpe) > 100:
            sharpe = 0.0
    else:
        sharpe = 0.0

    peak = np.maximum.accumulate(np.cumprod(1 + r))
    dd = (np.cumprod(1 + r) - peak) / peak
    max_dd = float(np.min(dd))
    calmar = ann_ret / abs(max_dd) if abs(max_dd) > 1e-10 else 0.0
    win_rate = float(np.mean(r > 0))

    return TrialResult(
        params=params,
        sharpe=round(sharpe, 4),
        total_return=round(total_ret, 4),
        max_drawdown=round(max_dd, 4),
        calmar=round(calmar, 4),
        win_rate=round(win_rate, 4),
    )


def grid_search(
    param_grid: dict[str, list],
    returns: np.ndarray,
    objective: Objective = Objective.MAX_SHARPE,
) -> OptimizationResult:
    """Exhaustive grid search over parameter combinations."""
    keys = list(param_grid.keys())
    values = list(param_grid.values())
    best: Optional[TrialResult] = None
    trials: list[TrialResult] = []
    scores = {Objective.MAX_SHARPE: "sharpe", Objective.MAX_CALMAR: "calmar",
              Objective.MIN_MDD: "max_drawdown", Objective.MAX_RETURN: "total_return"}

    for combo in product(*values):
        params = dict(zip(keys, combo))
        trial = evaluate_params(params, returns, objective)
        trials.append(trial)

        if best is None:
            best = trial
        else:
            attr = scores.get(objective, "sharpe")
            val, best_val = getattr(trial, attr), getattr(best, attr)
            if objective == Objective.MIN_MDD:
                if val < best_val:
                    best = trial
            elif val > best_val:
                best = trial

    if best is None:
        return OptimizationResult(best_params={}, best_score=0.0, all_trials=[], objective=objective)

    return OptimizationResult(
        best_params=best.params,
        best_score=getattr(best, scores.get(objective, "sharpe")),
        all_trials=trials,
        objective=objective,
    )


def walk_forward_optimize(
    param_grid: dict[str, list],
    returns: np.ndarray,
    n_folds: int = 5,
    objective: Objective = Objective.MAX_SHARPE,
) -> OptimizationResult:
    """
    Walk-forward parameter optimization to avoid overfitting.

    Splits data into n_folds. Trains on fold 0..k-1, tests on fold k.
    Returns best params across all walk-forward windows.
    """
    n = len(returns)
    if n < n_folds * 20:
        n_folds = max(2, n // 20)

    fold_size = n // (n_folds + 1)
    best_params = None
    best_fold_score = -float("inf") if objective != Objective.MIN_MDD else float("inf")
    all_trials: list[TrialResult] = []
    is_scores = []
    oos_scores = []

    for fold in range(1, n_folds + 1):
        train_end = fold * fold_size
        test_end = min((fold + 1) * fold_size, n)
        train_returns = returns[:train_end]
        test_returns = returns[train_end:test_end]

        if len(train_returns) < 10 or len(test_returns) < 5:
            continue

        fold_result = grid_search(param_grid, train_returns, objective)
        all_trials.extend(fold_result.all_trials)

        # Evaluate best params out-of-sample
        test_trial = evaluate_params(fold_result.best_params, test_returns, objective)
        scores = {Objective.MAX_SHARPE: "sharpe", Objective.MAX_CALMAR: "calmar",
                  Objective.MIN_MDD: "max_drawdown", Objective.MAX_RETURN: "total_return"}
        attr = scores.get(objective, "sharpe")
        fold_score = getattr(test_trial, attr)

        if objective == Objective.MIN_MDD:
            if fold_score < best_fold_score:
                best_fold_score = fold_score
                best_params = fold_result.best_params
        elif fold_score > best_fold_score:
            best_fold_score = fold_score
            best_params = fold_result.best_params

        is_scores.append(fold_result.best_score)
        oos_scores.append(fold_score)

    return OptimizationResult(
        best_params=best_params or {},
        best_score=best_fold_score,
        all_trials=all_trials,
        objective=objective,
        in_sample_score=round(float(np.mean(is_scores)), 4) if is_scores else 0.0,
        out_of_sample_score=round(float(np.mean(oos_scores)), 4) if oos_scores else 0.0,
    )
