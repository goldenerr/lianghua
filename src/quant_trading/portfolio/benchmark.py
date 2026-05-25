"""Benchmark registry and excess-return metrics for portfolio-002."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

import numpy as np

from .attribution import calculate_brinson


def _returns(name: str, values: Sequence[float]) -> np.ndarray:
    result = np.asarray(values, dtype=float)
    if result.ndim != 1 or result.size < 2:
        raise ValueError(f"{name} must contain at least two returns")
    if not np.isfinite(result).all():
        raise ValueError(f"{name} contains non-finite values")
    return result


@dataclass(frozen=True)
class BenchmarkMetrics:
    """Portfolio-relative metrics using aligned daily return observations."""

    benchmark_id: str
    observations: int
    annualized_excess_return: float
    tracking_error: float
    information_ratio: float
    active_share: float | None = None

    def to_dict(self) -> dict[str, object]:
        values = asdict(self)
        return {
            key: round(float(value), 12) if isinstance(value, float) else value
            for key, value in values.items()
        }


class BenchmarkRegistry:
    """In-memory registry for approved and time-aligned benchmark returns."""

    def __init__(self) -> None:
        self._returns_by_id: dict[str, np.ndarray] = {}

    def register(self, benchmark_id: str, daily_returns: Sequence[float]) -> None:
        if not benchmark_id.strip():
            raise ValueError("benchmark_id cannot be empty")
        self._returns_by_id[benchmark_id] = _returns("daily_returns", daily_returns).copy()

    def compare(
        self,
        benchmark_id: str,
        portfolio_returns: Sequence[float],
        portfolio_weights: Sequence[float] | None = None,
        benchmark_weights: Sequence[float] | None = None,
        periods_per_year: int = 252,
    ) -> BenchmarkMetrics:
        if periods_per_year <= 0:
            raise ValueError("periods_per_year must be positive")
        if benchmark_id not in self._returns_by_id:
            raise KeyError(f"benchmark not registered: {benchmark_id}")
        portfolio = _returns("portfolio_returns", portfolio_returns)
        benchmark = self._returns_by_id[benchmark_id]
        if portfolio.size != benchmark.size:
            raise ValueError("portfolio and benchmark observations must align")
        active = portfolio - benchmark
        tracking_error = float(active.std(ddof=1) * np.sqrt(periods_per_year))
        excess = float(active.mean() * periods_per_year)
        information_ratio = excess / tracking_error if tracking_error > 0 else 0.0
        active_share = None
        if portfolio_weights is not None or benchmark_weights is not None:
            if portfolio_weights is None or benchmark_weights is None:
                raise ValueError("both portfolio_weights and benchmark_weights are required")
            p_weight = np.asarray(portfolio_weights, dtype=float)
            b_weight = np.asarray(benchmark_weights, dtype=float)
            if p_weight.shape != b_weight.shape or p_weight.ndim != 1 or p_weight.size == 0:
                raise ValueError("aligned one-dimensional weights are required")
            if not np.isclose(p_weight.sum(), 1.0) or not np.isclose(b_weight.sum(), 1.0):
                raise ValueError("weights must sum to 1")
            active_share = float(0.5 * np.abs(p_weight - b_weight).sum())
        return BenchmarkMetrics(
            benchmark_id=benchmark_id,
            observations=int(portfolio.size),
            annualized_excess_return=excess,
            tracking_error=tracking_error,
            information_ratio=information_ratio,
            active_share=active_share,
        )

    def compare_all(
        self,
        portfolio_returns: Sequence[float],
        periods_per_year: int = 252,
    ) -> Mapping[str, BenchmarkMetrics]:
        return {
            benchmark_id: self.compare(benchmark_id, portfolio_returns, periods_per_year=periods_per_year)
            for benchmark_id in sorted(self._returns_by_id)
        }


def brinson_attribution(
    portfolio_weights: Sequence[float],
    benchmark_weights: Sequence[float],
    sector_returns: Sequence[float],
    benchmark_sector_returns: Sequence[float],
) -> dict[str, float]:
    """Compatibility API delegating to the validated implementation."""

    return calculate_brinson(
        portfolio_weights,
        benchmark_weights,
        sector_returns,
        benchmark_sector_returns,
    ).to_dict()
