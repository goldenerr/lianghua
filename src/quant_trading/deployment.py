"""Controlled canary and blue-green deployment decisions for deploy-001.

This module decides whether a deployment may advance; it never deploys or
trades automatically. Production rollout still requires artifact signature
verification and the documented approval workflow.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from quant_trading.core.audit import AuditBus


class DeployStrategy(str, Enum):
    CANARY = "canary"
    BLUE_GREEN = "blue_green"
    ROLLING = "rolling"


class DeploymentDecision(str, Enum):
    HOLD = "hold"
    ADVANCE = "advance"
    COMPLETE = "complete"
    ROLLBACK = "rollback"


@dataclass(frozen=True)
class CanaryMetrics:
    """Production signals evaluated for each canary observation window."""

    error_rate: float
    p99_latency_ms: float
    sharpe_change_pct: float

    def __post_init__(self) -> None:
        if not 0 <= self.error_rate <= 1:
            raise ValueError("error_rate must be between 0 and 1")
        if self.p99_latency_ms < 0:
            raise ValueError("p99_latency_ms must not be negative")


@dataclass
class CanaryController:
    """Advance canary stages only after a healthy full validation window."""

    stages: tuple[int, ...] = (5, 10, 50, 100)
    error_rate_threshold: float = 0.05
    p99_latency_threshold_ms: float = 200.0
    sharpe_decline_threshold_pct: float = -0.30
    validation_hours: float = 24.0
    audit_bus: AuditBus | None = None
    percentage: int = field(init=False)
    rolled_back: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not self.stages or self.stages[0] != 5 or self.stages[-1] != 100:
            raise ValueError("canary stages must start at 5 and end at 100 percent")
        if tuple(sorted(set(self.stages))) != self.stages:
            raise ValueError("canary stages must be strictly increasing")
        if self.validation_hours <= 0:
            raise ValueError("validation_hours must be positive")
        self.percentage = self.stages[0]

    def evaluate(self, metrics: CanaryMetrics, observation_hours: float) -> DeploymentDecision:
        """Evaluate one rollout observation window and emit an auditable decision."""
        reasons = self._breach_reasons(metrics)
        if reasons:
            self.percentage = 0
            self.rolled_back = True
            return self._record(DeploymentDecision.ROLLBACK, metrics, observation_hours, reasons)

        if self.rolled_back:
            return self._record(
                DeploymentDecision.ROLLBACK,
                metrics,
                observation_hours,
                ["deployment previously rolled back; manual restart required"],
            )
        if observation_hours < self.validation_hours:
            return self._record(DeploymentDecision.HOLD, metrics, observation_hours, [])
        if self.percentage == self.stages[-1]:
            return self._record(DeploymentDecision.COMPLETE, metrics, observation_hours, [])

        current_index = self.stages.index(self.percentage)
        self.percentage = self.stages[current_index + 1]
        decision = DeploymentDecision.COMPLETE if self.percentage == 100 else DeploymentDecision.ADVANCE
        return self._record(decision, metrics, observation_hours, [])

    def _breach_reasons(self, metrics: CanaryMetrics) -> list[str]:
        reasons = []
        if metrics.error_rate > self.error_rate_threshold:
            reasons.append("error_rate")
        if metrics.p99_latency_ms > self.p99_latency_threshold_ms:
            reasons.append("p99_latency")
        if metrics.sharpe_change_pct < self.sharpe_decline_threshold_pct:
            reasons.append("sharpe_decline")
        return reasons

    def _record(
        self,
        decision: DeploymentDecision,
        metrics: CanaryMetrics,
        observation_hours: float,
        reasons: list[str],
    ) -> DeploymentDecision:
        if self.audit_bus is not None:
            self.audit_bus.record(
                "canary_decision",
                "deployment",
                {
                    "decision": decision.value,
                    "traffic_percentage": self.percentage,
                    "observation_hours": observation_hours,
                    "metrics": {
                        "error_rate": metrics.error_rate,
                        "p99_latency_ms": metrics.p99_latency_ms,
                        "sharpe_change_pct": metrics.sharpe_change_pct,
                    },
                    "reasons": reasons,
                },
            )
        return decision
