"""Online drift detection and fail-safe model rollback controls for ml-002."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from quant_trading.core.audit import AuditBus


def detect_concept_drift(
    old_data: np.ndarray | Sequence[float],
    new_data: np.ndarray | Sequence[float],
    window: int = 50,
    threshold: float = 0.5,
) -> dict[str, float | bool]:
    """Compare rolling distributions with a standardized mean-change alarm."""

    baseline = np.asarray(old_data, dtype=float).reshape(-1)
    current = np.asarray(new_data, dtype=float).reshape(-1)
    if window < 2 or baseline.size < window or current.size < window:
        raise ValueError("both samples must contain at least window observations")
    if not np.isfinite(baseline).all() or not np.isfinite(current).all():
        raise ValueError("samples contain non-finite values")
    baseline_window = baseline[-window:]
    current_window = current[-window:]
    pooled_scale = np.sqrt((baseline_window.var(ddof=1) + current_window.var(ddof=1)) / 2)
    drift = abs(float(current_window.mean() - baseline_window.mean())) / max(
        float(pooled_scale), 1e-12
    )
    return {"drift_score": round(drift, 6), "significant": drift > threshold}


@dataclass
class ShadowDeployment:
    stable_version: str
    candidate_version: str | None = None
    active_version: str | None = None

    def __post_init__(self) -> None:
        self.active_version = self.active_version or self.stable_version


class ModelRollbackController:
    """Run candidate models in shadow/paper mode and fail closed on drift."""

    def __init__(self, deployment: ShadowDeployment, audit_bus: AuditBus | None = None) -> None:
        self.deployment = deployment
        self.audit = audit_bus or AuditBus()

    def register_candidate(self, version: str) -> None:
        if not version.strip() or version == self.deployment.stable_version:
            raise ValueError("candidate must identify a new model version")
        self.deployment.candidate_version = version
        self.audit.record("model_candidate_registered", "model_rollback", {"version": version})

    def approve_candidate(self, approved_by: str, approval_ref: str) -> None:
        if self.deployment.candidate_version is None:
            raise ValueError("no candidate model registered")
        if not approved_by.strip() or not approval_ref.strip():
            raise PermissionError("approval identity and reference are required")
        self.deployment.active_version = self.deployment.candidate_version
        self.audit.record(
            "model_candidate_activated",
            "model_rollback",
            {
                "version": self.deployment.active_version,
                "approved_by": approved_by,
                "approval_ref": approval_ref,
            },
        )

    def rollback_on_drift(self, report: dict[str, float | bool]) -> bool:
        if report.get("significant") is not True:
            return False
        failed_version = self.deployment.active_version
        self.deployment.active_version = self.deployment.stable_version
        self.audit.record(
            "model_rollback_triggered",
            "model_rollback",
            {"failed_version": failed_version, "stable_version": self.deployment.stable_version},
        )
        return True
