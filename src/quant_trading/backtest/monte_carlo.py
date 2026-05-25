"""
Monte Carlo simulation and stress testing for backtest robustness.

AGENTS.md §5: Monte Carlo 1000 runs
AGENTS.md §6: Stress test scenarios (2020.3, 2015.8, 2022 bear)
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from .engine import (
    BacktestConfig,
    MetricsCalculator,
    PerformanceMetrics,
)

logger = logging.getLogger(__name__)


class MonteCarloSimulator:
    """
    Monte Carlo simulation on trade sequences.

    AGENTS.md §5: 1000 runs of resampled trade sequences.
    Assesses strategy robustness beyond single path.
    """

    def __init__(self, config: BacktestConfig) -> None:
        self.config = config
        self.rng = np.random.RandomState(42) if config.deterministic else np.random.RandomState()

    def run(self, trades: pd.DataFrame) -> dict[str, Any]:
        """
        Run Monte Carlo on trade PnLs.

        Returns distribution of key metrics across simulations.
        """
        if trades.empty or "pnl" not in trades.columns:
            return {"error": "No trade data with pnl column"}

        pnls = trades["pnl"].values
        n_trades = len(pnls)

        sharpe_dist: list[float] = []
        mdd_dist: list[float] = []
        ret_dist: list[float] = []

        for _ in range(self.config.mc_runs):
            # Block resample (preserve autocorrelation structure)
            block_size = min(self.config.mc_resample_block_size, n_trades)
            n_blocks = n_trades // block_size
            resampled: list[float] = []
            for __ in range(n_blocks):
                idx = self.rng.randint(0, n_trades - block_size + 1)
                resampled.extend(pnls[idx : idx + block_size])
            # Handle remainder
            remainder = n_trades - len(resampled)
            if remainder > 0:
                idx = self.rng.randint(0, n_trades - remainder + 1)
                resampled.extend(pnls[idx : idx + remainder])

            resampled_arr = np.array(resampled)
            equity = 1.0 + np.cumsum(resampled_arr)
            equity_series = pd.Series(equity)

            metrics = MetricsCalculator.from_equity_curve(equity_series)
            sharpe_dist.append(metrics.sharpe_ratio)
            mdd_dist.append(metrics.max_drawdown)
            ret_dist.append(metrics.total_return)

        return {
            "sharpe": {
                "mean": float(np.mean(sharpe_dist)),
                "median": float(np.median(sharpe_dist)),
                "p5": float(np.percentile(sharpe_dist, 5)),
                "p95": float(np.percentile(sharpe_dist, 95)),
                "std": float(np.std(sharpe_dist)),
            },
            "mdd": {
                "mean": float(np.mean(mdd_dist)),
                "p5": float(np.percentile(mdd_dist, 5)),
                "p95": float(np.percentile(mdd_dist, 95)),
            },
            "total_return": {
                "mean": float(np.mean(ret_dist)),
                "p5": float(np.percentile(ret_dist, 5)),
                "p95": float(np.percentile(ret_dist, 95)),
            },
            "n_simulations": self.config.mc_runs,
        }


class StressTestRunner:
    """
    Predefined stress scenarios and historical crisis replay.

    AGENTS.md §6: 压力测试含 2020.3, 2015.8, 2022 bear market scenes.
    """

    # Pre-calculated stress multipliers for known crisis periods
    SCENARIOS: ClassVar[dict[str, dict[str, Any]]] = {
        "covid_2020": {
            "description": "COVID-19 crash (Feb-Mar 2020)",
            "shock_pct": -0.34,  # S&P 500 drawdown
            "vol_multiplier": 5.0,
            "duration_days": 23,
        },
        "china_2015": {
            "description": "2015 China stock market crash",
            "shock_pct": -0.45,  # CSI 300 drawdown
            "vol_multiplier": 6.0,
            "duration_days": 50,
        },
        "bear_2022": {
            "description": "2022 global bear market",
            "shock_pct": -0.25,
            "vol_multiplier": 3.0,
            "duration_days": 200,
        },
        "flash_crash": {
            "description": "Single-day flash crash",
            "shock_pct": -0.15,
            "vol_multiplier": 10.0,
            "duration_days": 1,
        },
        "black_swan": {
            "description": "Black swan tail event (3-sigma)",
            "shock_pct": -0.50,
            "vol_multiplier": 8.0,
            "duration_days": 5,
        },
    }

    @classmethod
    def apply_scenario(
        cls,
        equity_curve: pd.Series,
        scenario_name: str,
    ) -> PerformanceMetrics:
        """
        Apply a stress scenario to an equity curve and measure impact.

        Simulates the scenario by applying a sequence of shocks
        matching the scenario's drawdown and volatility profile.
        """
        if scenario_name not in cls.SCENARIOS:
            raise KeyError(f"Unknown scenario: {scenario_name}. Available: {list(cls.SCENARIOS)}")

        scenario = cls.SCENARIOS[scenario_name]
        eq = equity_curve.copy()
        n = len(eq)
        duration = min(int(scenario["duration_days"]), n)

        # Find insertion point (middle of series)
        start_idx = max(0, n // 2 - duration // 2)
        end_idx = min(start_idx + duration, n)

        # Generate shock path
        rng = np.random.RandomState(42)
        shock_pct = float(scenario["shock_pct"])
        vol_multiplier = float(scenario["vol_multiplier"])
        daily_shock = -(1 - (1 - shock_pct) ** (1 / duration))
        daily_vol = daily_shock * vol_multiplier / 3.0

        shocks = rng.normal(loc=daily_shock, scale=daily_vol, size=end_idx - start_idx)

        # Apply to equity
        for i, idx in enumerate(range(start_idx, end_idx)):
            eq.iloc[idx] = eq.iloc[idx] * (1 + shocks[i])

        return MetricsCalculator.from_equity_curve(eq)

    @classmethod
    def run_all_scenarios(cls, equity_curve: pd.Series) -> dict[str, dict[str, Any]]:
        """Run all predefined stress scenarios."""
        results: dict[str, dict[str, Any]] = {}
        for name in cls.SCENARIOS:
            metrics = cls.apply_scenario(equity_curve, name)
            results[name] = {
                "description": cls.SCENARIOS[name]["description"],
                "sharpe": round(metrics.sharpe_ratio, 4),
                "mdd_pct": round(metrics.max_drawdown * 100, 2),
                "total_return_pct": round(metrics.total_return * 100, 2),
            }
        return results
