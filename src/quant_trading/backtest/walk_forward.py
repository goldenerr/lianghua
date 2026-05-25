"""
Walk-Forward validation and forward-looking bias detection.

AGENTS.md §5: Must run Walk-Forward with 5+ folds, 20% OOS each.
AGENTS.md §5: Purged Cross-Validation to prevent data leakage.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import numpy as np
import pandas as pd

from .engine import (
    BacktestConfig,
    BacktestEngine,
    BacktestResult,
    PerformanceMetrics,
)

logger = logging.getLogger(__name__)


class WalkForwardValidator:
    """
    Walk-Forward analysis with Purged Cross-Validation.

    AGENTS.md §5:
      "至少 5 段，每段样本外比例 20%，保证时间顺序不泄露"
      "样本外 Sharpe 下降不超过样本内的 30%"
      "关键参数变化 ±10% 时绩效指标波动 ≤ 15%"
    """

    def __init__(
        self,
        engine: BacktestEngine,
        config: BacktestConfig,
        purge_days: int = 5,
    ):
        self.engine = engine
        self.config = config
        self.purge_days = purge_days  # Days to purge between train/test

    def run(
        self,
        data: dict[str, pd.DataFrame],
        strategy: Callable,
    ) -> BacktestResult:
        """
        Execute walk-forward validation.

        Returns a BacktestResult with wf_folds and wf_oos_metrics populated.
        """
        if not data:
            return BacktestResult(errors=["No data provided"])

        # Get date range from first symbol's data
        first_df = next(iter(data.values()))
        all_dates = sorted(first_df.index.unique())
        n = len(all_dates)

        if n < self.config.wf_folds * 2:
            return BacktestResult(errors=[f"Not enough data ({n} days) for {self.config.wf_folds} folds"])

        fold_size = n // (self.config.wf_folds + 1)
        results: list[PerformanceMetrics] = []
        warnings: list[str] = []

        for fold in range(self.config.wf_folds):
            # Train: first (fold+1) segments
            train_end_idx = (fold + 1) * fold_size - self.purge_days
            # Test: next segment
            test_start_idx = train_end_idx + self.purge_days + 1
            test_end_idx = min(test_start_idx + int(fold_size * (1 + self.config.wf_oos_pct)), n - 1)

            if test_end_idx <= test_start_idx:
                break

            train_dates = all_dates[:train_end_idx + 1]
            test_dates = all_dates[test_start_idx:test_end_idx + 1]

            # Split data
            train_data = {
                sym: df.loc[df.index.isin(train_dates)]
                for sym, df in data.items()
            }
            test_data = {
                sym: df.loc[df.index.isin(test_dates)]
                for sym, df in data.items()
            }

            if any(df.empty for df in train_data.values()):
                continue
            if any(df.empty for df in test_data.values()):
                continue

            # Run on OOS
            oos_config = BacktestConfig(
                **{**self.config.__dict__,
                   "start_date": test_dates[0].date() if hasattr(test_dates[0], "date") else None,
                   "end_date": test_dates[-1].date() if hasattr(test_dates[-1], "date") else None,
                }
            )
            oos_result = self.engine.run(test_data, strategy, oos_config)
            results.append(oos_result.metrics)

        if not results:
            return BacktestResult(errors=["No valid walk-forward folds"])

        # Aggregate OOS metrics
        oos_returns = [m.annualized_return for m in results]
        oos_sharpes = [m.sharpe_ratio for m in results]

        avg_oos = PerformanceMetrics(
            annualized_return=np.mean(oos_returns),
            sharpe_ratio=np.mean(oos_sharpes),
            max_drawdown=max(m.max_drawdown for m in results),
            win_rate=np.mean([m.win_rate for m in results]),
            total_trades=sum(m.total_trades for m in results),
        )

        # Full-sample metrics for comparison
        full_result = self.engine.run(data, strategy, self.config)
        full_metrics = full_result.metrics

        # Check OOS decay (AGENTS.md §5: Sharpe decline ≤ 30%)
        if full_metrics.sharpe_ratio > 0:
            sharpe_decay = 1 - (avg_oos.sharpe_ratio / full_metrics.sharpe_ratio)
            if sharpe_decay > self.config.max_sharpe_decay_oos:
                warnings.append(
                    f"OOS Sharpe decay {sharpe_decay:.1%} exceeds "
                    f"maximum {self.config.max_sharpe_decay_oos:.1%} — possible overfitting"
                )

        return BacktestResult(
            metrics=full_metrics,
            wf_folds=results,
            wf_oos_metrics=avg_oos,
            equity_curve=full_result.equity_curve,
            trades=full_result.trades,
            config=self.config,
            warnings=warnings,
        )

    def sensitivity_test(
        self,
        data: dict[str, pd.DataFrame],
        strategy_factory: Callable[[dict], Callable],
        base_params: dict,
        param_name: str,
        perturbation_pct: float = 0.10,
    ) -> dict:
        """
        Parameter sensitivity test (AGENTS.md §5).
        Key parameter ±10% → performance change ≤ 15%.
        """
        results = {}

        for pct in [-perturbation_pct, 0, perturbation_pct]:
            params = dict(base_params)
            params[param_name] = base_params[param_name] * (1 + pct)
            strategy = strategy_factory(params)
            result = self.engine.run(data, strategy, self.config)
            results[pct] = result.metrics

        # Compute sensitivity
        base_sharpe = results[0].sharpe_ratio
        up_sharpe = results[perturbation_pct].sharpe_ratio
        down_sharpe = results[-perturbation_pct].sharpe_ratio

        sensitivity = max(
            abs(up_sharpe - base_sharpe) / max(abs(base_sharpe), 1e-10),
            abs(down_sharpe - base_sharpe) / max(abs(base_sharpe), 1e-10),
        )

        return {
            "param": param_name,
            "perturbation_pct": perturbation_pct,
            "sensitivity": sensitivity,
            "passed": sensitivity <= self.config.max_param_sensitivity,
            "results": {str(k): v.to_dict() for k, v in results.items()},
        }
