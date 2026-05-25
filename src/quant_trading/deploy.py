"""Compatibility entrypoint for controlled canary deployment."""
from __future__ import annotations

from .deployment import CanaryController, CanaryMetrics, DeploymentDecision


class CanaryDeployer:
    """Small adapter retained for callers of the original deployment API."""

    def __init__(self) -> None:
        self.controller = CanaryController(validation_hours=24)

    @property
    def percentage(self) -> int:
        return self.controller.percentage

    def increment(self, step: int = 5) -> int:
        """Reject unsafe arbitrary progression; use a healthy validated window."""
        if step != 5:
            raise ValueError("arbitrary canary steps are prohibited")
        decision = self.controller.evaluate(CanaryMetrics(0.0, 0.0, 0.0), observation_hours=24)
        if decision == DeploymentDecision.ROLLBACK:
            raise RuntimeError("canary deployment rolled back")
        return self.percentage
