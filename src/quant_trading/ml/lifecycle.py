"""Model validation and controlled lifecycle primitives for ml-001."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum

import numpy as np

from quant_trading.core.audit import AuditBus

UTC = timezone.utc


class ModelStage(str, Enum):
    DEV = "dev"
    SHADOW = "shadow"
    PAPER = "paper"
    PROD = "prod"
    RETIRED = "retired"


@dataclass(frozen=True)
class ModelVersion:
    name: str
    version: str
    stage: ModelStage = ModelStage.DEV
    feature_names: tuple[str, ...] = ()
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    metrics: Mapping[str, float] = field(default_factory=dict)
    signature_verified: bool = False


@dataclass(frozen=True)
class DriftReport:
    psi_by_feature: dict[str, float]
    maximum_psi: float
    drifted: bool
    threshold: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _matrix(values: np.ndarray | Sequence[float], name: str) -> np.ndarray:
    matrix = np.asarray(values, dtype=float)
    if matrix.ndim == 1:
        matrix = matrix[:, None]
    if matrix.ndim != 2 or matrix.shape[0] < 2:
        raise ValueError(f"{name} must contain at least two observations")
    if not np.isfinite(matrix).all():
        raise ValueError(f"{name} contains non-finite values")
    return matrix


def detect_feature_drift(
    reference: np.ndarray | Sequence[float],
    current: np.ndarray | Sequence[float],
    threshold: float = 0.2,
    bins: int = 10,
    feature_names: Sequence[str] | None = None,
) -> dict[str, object]:
    """Compute population stability index (PSI) for each feature.

    Bin boundaries are learned from the reference sample only, preventing
    evaluation data from altering the baseline distribution.
    """

    if threshold <= 0 or bins < 2:
        raise ValueError("threshold must be positive and bins must be at least two")
    baseline = _matrix(reference, "reference")
    observed = _matrix(current, "current")
    if baseline.shape[1] != observed.shape[1]:
        raise ValueError("reference and current feature dimensions must match")
    names = tuple(feature_names or (f"feature_{idx}" for idx in range(baseline.shape[1])))
    if len(names) != baseline.shape[1]:
        raise ValueError("feature_names length does not match feature dimensions")
    psi_by_feature: dict[str, float] = {}
    for idx, name in enumerate(names):
        boundaries = np.unique(np.quantile(baseline[:, idx], np.linspace(0, 1, bins + 1)))
        if boundaries.size < 2:
            psi_by_feature[name] = (
                0.0 if np.allclose(baseline[:, idx], observed[:, idx]) else float("inf")
            )
            continue
        boundaries[0], boundaries[-1] = -np.inf, np.inf
        expected, _ = np.histogram(baseline[:, idx], bins=boundaries)
        actual, _ = np.histogram(observed[:, idx], bins=boundaries)
        expected_pct = np.clip(expected / expected.sum(), 1e-6, None)
        actual_pct = np.clip(actual / actual.sum(), 1e-6, None)
        psi = np.sum((actual_pct - expected_pct) * np.log(actual_pct / expected_pct))
        psi_by_feature[name] = round(float(psi), 6)
    maximum = max(psi_by_feature.values())
    return DriftReport(psi_by_feature, maximum, maximum > threshold, threshold).to_dict()


def evaluate_adversarial_robustness(
    predictor: Callable[[np.ndarray], np.ndarray],
    features: np.ndarray,
    epsilon: float = 1e-3,
    tolerance: float = 0.05,
    required_pass_rate: float = 0.95,
) -> dict[str, float | bool]:
    """Evaluate signal stability under deterministic bounded perturbations."""

    if epsilon <= 0 or tolerance < 0 or not 0 <= required_pass_rate <= 1:
        raise ValueError("invalid robustness parameters")
    data = _matrix(features, "features")
    baseline = np.asarray(predictor(data), dtype=float).reshape(-1)
    positive = np.asarray(predictor(data + epsilon), dtype=float).reshape(-1)
    negative = np.asarray(predictor(data - epsilon), dtype=float).reshape(-1)
    if (
        baseline.size != data.shape[0]
        or positive.shape != baseline.shape
        or negative.shape != baseline.shape
    ):
        raise ValueError("predictor must emit one prediction per observation")
    worst_change = np.maximum(np.abs(positive - baseline), np.abs(negative - baseline))
    pass_rate = float(np.mean(worst_change <= tolerance))
    return {
        "pass_rate": round(pass_rate, 6),
        "maximum_prediction_change": round(float(worst_change.max()), 6),
        "passed": pass_rate >= required_pass_rate,
    }


def evaluate_regime_bias(
    predictions: Sequence[float],
    actuals: Sequence[float],
    regimes: Sequence[str],
    maximum_error_gap: float = 0.2,
) -> dict[str, object]:
    """Flag materially different prediction errors across market regimes."""

    forecast = np.asarray(predictions, dtype=float)
    realized = np.asarray(actuals, dtype=float)
    if forecast.shape != realized.shape or forecast.size != len(regimes) or forecast.size == 0:
        raise ValueError("aligned predictions, actuals, and regimes are required")
    errors: dict[str, float] = {}
    for regime in sorted(set(regimes)):
        mask = np.asarray([label == regime for label in regimes])
        errors[regime] = round(float(np.mean(np.abs(forecast[mask] - realized[mask]))), 6)
    error_gap = max(errors.values()) - min(errors.values()) if len(errors) > 1 else 0.0
    return {
        "mean_absolute_error_by_regime": errors,
        "error_gap": error_gap,
        "biased": error_gap > maximum_error_gap,
    }


def shadow_test(
    prod_model: Callable, shadow_model: Callable, data: np.ndarray
) -> dict[str, object]:
    """Compare production and candidate predictions without placing orders."""

    values = _matrix(data, "data")
    prod_prediction = np.asarray(prod_model(values), dtype=float).reshape(-1)
    shadow_prediction = np.asarray(shadow_model(values), dtype=float).reshape(-1)
    if prod_prediction.shape != shadow_prediction.shape or prod_prediction.size != values.shape[0]:
        raise ValueError("models must emit aligned per-observation predictions")
    delta = shadow_prediction - prod_prediction
    return {
        "prod_prediction": prod_prediction.tolist(),
        "shadow_prediction": shadow_prediction.tolist(),
        "mean_absolute_delta": round(float(np.mean(np.abs(delta))), 6),
    }


class ModelPromotionGate:
    """Requires signed candidates, paper evidence, and recorded risk approval."""

    def __init__(self, audit_bus: AuditBus | None = None, minimum_paper_days: int = 90) -> None:
        self.audit = audit_bus or AuditBus()
        self.minimum_paper_days = minimum_paper_days

    def promote_to_production(
        self,
        model: ModelVersion,
        paper_days: int,
        approved_by: str,
        approval_ref: str,
    ) -> ModelVersion:
        if model.stage != ModelStage.PAPER or not model.signature_verified:
            raise PermissionError("only signed paper-tested models may be promoted")
        if paper_days < self.minimum_paper_days:
            raise PermissionError("minimum paper trading duration not satisfied")
        if not approved_by.strip() or not approval_ref.strip():
            raise PermissionError("risk approval and reference are required")
        promoted = ModelVersion(
            name=model.name,
            version=model.version,
            stage=ModelStage.PROD,
            feature_names=model.feature_names,
            metrics=model.metrics,
            signature_verified=True,
        )
        self.audit.record(
            "model_production_approved",
            "model_promotion_gate",
            {
                "model": model.name,
                "version": model.version,
                "approved_by": approved_by,
                "approval_ref": approval_ref,
            },
        )
        return promoted
