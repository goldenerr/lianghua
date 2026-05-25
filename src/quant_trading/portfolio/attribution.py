"""Deterministic performance attribution primitives for portfolio-002.

The functions in this module operate on supplied, approved market data. They
do not silently fetch or substitute benchmark returns, which is important for
backtest/live reproducibility.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date

import numpy as np

_DIMENSIONS = frozenset({"style", "industry_factor", "timing", "security_selection"})


def _validated_vector(name: str, values: Sequence[float], size: int | None = None) -> np.ndarray:
    vector = np.asarray(values, dtype=float)
    if vector.ndim != 1 or vector.size == 0:
        raise ValueError(f"{name} must be a non-empty one-dimensional sequence")
    if size is not None and vector.size != size:
        raise ValueError(f"{name} length must be {size}, got {vector.size}")
    if not np.isfinite(vector).all():
        raise ValueError(f"{name} contains non-finite values")
    return vector


@dataclass(frozen=True)
class BrinsonResult:
    """Single classification-level Brinson-Fachler decomposition."""

    allocation_effect: float
    selection_effect: float
    interaction_effect: float
    total_excess: float

    def to_dict(self) -> dict[str, float]:
        return {key: round(float(value), 12) for key, value in asdict(self).items()}


def calculate_brinson(
    portfolio_weights: Sequence[float],
    benchmark_weights: Sequence[float],
    portfolio_returns: Sequence[float],
    benchmark_returns: Sequence[float],
) -> BrinsonResult:
    """Calculate Brinson effects using classification bucket weights/returns.

    Both portfolios must be fully allocated. Leverage attribution must be
    handled explicitly before calling this function rather than hidden in the
    decomposition.
    """

    p_weight = _validated_vector("portfolio_weights", portfolio_weights)
    b_weight = _validated_vector("benchmark_weights", benchmark_weights, p_weight.size)
    p_return = _validated_vector("portfolio_returns", portfolio_returns, p_weight.size)
    b_return = _validated_vector("benchmark_returns", benchmark_returns, p_weight.size)
    if not np.isclose(p_weight.sum(), 1.0, atol=1e-8):
        raise ValueError("portfolio_weights must sum to 1")
    if not np.isclose(b_weight.sum(), 1.0, atol=1e-8):
        raise ValueError("benchmark_weights must sum to 1")

    benchmark_total_return = float(np.dot(b_weight, b_return))
    allocation = float(np.dot(p_weight - b_weight, b_return - benchmark_total_return))
    selection = float(np.dot(b_weight, p_return - b_return))
    interaction = float(np.dot(p_weight - b_weight, p_return - b_return))
    total = allocation + selection + interaction
    direct_excess = float(np.dot(p_weight, p_return) - np.dot(b_weight, b_return))
    if not np.isclose(total, direct_excess, atol=1e-10):
        raise RuntimeError("Brinson decomposition failed to reconcile to excess return")
    return BrinsonResult(allocation, selection, interaction, total)


def brinson_attribute(
    portfolio_weights: Sequence[float],
    benchmark_weights: Sequence[float],
    returns: Sequence[float],
    benchmark_returns: Sequence[float] | None = None,
) -> dict[str, float]:
    """Backward-compatible wrapper around the audited Brinson calculation.

    For compatibility only, a missing benchmark return series is treated as
    zero return. New call sites should always provide ``benchmark_returns``.
    """

    reference = benchmark_returns if benchmark_returns is not None else [0.0] * len(returns)
    result = calculate_brinson(portfolio_weights, benchmark_weights, returns, reference)
    values = result.to_dict()
    return {
        **values,
        "allocation": values["allocation_effect"],
        "selection": values["selection_effect"],
        "total": values["total_excess"],
    }


@dataclass(frozen=True)
class AttributionReport:
    """Daily or weekly explainable attribution report."""

    as_of: date
    frequency: str
    benchmark_id: str
    effects_by_dimension: Mapping[str, BrinsonResult]

    def __post_init__(self) -> None:
        if self.frequency not in {"daily", "weekly"}:
            raise ValueError("frequency must be daily or weekly")
        unsupported = set(self.effects_by_dimension) - _DIMENSIONS
        if unsupported:
            raise ValueError(f"unsupported attribution dimensions: {sorted(unsupported)}")
        if not self.effects_by_dimension:
            raise ValueError("effects_by_dimension cannot be empty")

    def to_dict(self) -> dict[str, object]:
        """Serialize without summing overlapping classification dimensions."""

        return {
            "as_of": self.as_of.isoformat(),
            "frequency": self.frequency,
            "benchmark_id": self.benchmark_id,
            "effects_by_dimension": {
                dimension: result.to_dict()
                for dimension, result in sorted(self.effects_by_dimension.items())
            },
        }
