"""Controlled canary and blue-green deployment decisions for deploy-001.

This module decides whether a deployment may advance; it never deploys or
trades automatically. Production rollout still requires artifact signature
verification and the documented approval workflow.
"""

from __future__ import annotations

from collections.abc import Mapping
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


class ProductionReadinessError(RuntimeError):
    """Raised when production release evidence is missing or untrusted."""


@dataclass(frozen=True)
class EvidenceRequirement:
    """One externally verifiable production release prerequisite."""

    key: str
    description: str
    allowed_prefixes: tuple[str, ...]

    def validate(self, evidence: Mapping[str, str]) -> str | None:
        reference = evidence.get(self.key, "").strip()
        if not reference:
            return f"{self.key}: missing {self.description}"
        lowered = reference.lower()
        placeholders = ("todo", "pending", "tbd", "local", "mock", "dummy", "sample")
        if any(token in lowered for token in placeholders):
            return f"{self.key}: placeholder/local evidence is not acceptable"
        if not reference.startswith(self.allowed_prefixes):
            allowed = ", ".join(self.allowed_prefixes)
            return f"{self.key}: evidence must use one of {allowed}"
        return None


@dataclass(frozen=True)
class ProductionReadinessReport:
    """Hash/audit-friendly result for production gate evaluation."""

    passed: bool
    blockers: tuple[str, ...]
    evidence_keys: tuple[str, ...]


PRODUCTION_EVIDENCE_REQUIREMENTS: tuple[EvidenceRequirement, ...] = (
    EvidenceRequirement(
        "external_worm_archive",
        "external immutable WORM archive attestation",
        ("worm://", "s3-object-lock://", "vault-audit://"),
    ),
    EvidenceRequirement(
        "secret_manager",
        "approved production secret-manager resolver evidence",
        ("vault://", "aws-sm://", "sealed://"),
    ),
    EvidenceRequirement(
        "approval_service",
        "risk/configuration approval service evidence",
        ("approval://", "jira://", "servicenow://", "git-pr://"),
    ),
    EvidenceRequirement(
        "real_market_data_provider",
        "approved non-mock market-data provider evidence",
        ("provider://", "exchange-feed://", "vendor-feed://"),
    ),
    EvidenceRequirement(
        "provider_entitlement",
        "data-provider entitlement, license and redistribution evidence",
        ("entitlement://", "provider://", "contract://", "vendor-contract://"),
    ),
    EvidenceRequirement(
        "alt_archive_readiness",
        "archive-complete alternative-data readiness artifact",
        ("artifact://", "ci-artifact://", "worm://", "s3-object-lock://"),
    ),
    EvidenceRequirement(
        "exchange_position_provider",
        "exchange-backed reconciled position provider evidence",
        ("exchange://", "broker://"),
    ),
    EvidenceRequirement(
        "broker_borrow_availability",
        "broker-backed borrow availability feed and account binding evidence",
        ("broker://", "borrow-feed://", "exchange://", "entitlement://"),
    ),
    EvidenceRequirement(
        "paper_trading_90d",
        "three-month paper-trading gate evidence using real market data",
        ("paper://", "artifact://", "ci-artifact://", "report://"),
    ),
    EvidenceRequirement(
        "signed_plugin_review",
        "signed plugin load and code-review evidence",
        ("signature://", "cosign://", "git-pr://"),
    ),
    EvidenceRequirement(
        "production_calendar",
        "approved production exchange calendar evidence",
        ("calendar://", "exchange-calendar://", "vendor-calendar://"),
    ),
    EvidenceRequirement(
        "rollover_runbook",
        "approved futures rollover execution runbook/evidence",
        ("rollover://", "runbook://", "approval://"),
    ),
    EvidenceRequirement(
        "capacity_benchmark",
        "production-like capacity benchmark report",
        ("benchmark://", "grafana://", "artifact://"),
    ),
    EvidenceRequirement(
        "failover_dr_test",
        "multi-region failover and DR test report",
        ("dr-test://", "failover://", "artifact://"),
    ),
)


def evaluate_production_readiness(
    evidence: Mapping[str, str],
    *,
    audit_bus: AuditBus | None = None,
) -> ProductionReadinessReport:
    """Validate external production evidence without accepting local substitutes.

    The system may have local adapters and tests, but production release requires
    independently approved evidence references. Missing or placeholder evidence
    remains a blocker rather than being silently downgraded to a warning.
    """

    blockers = tuple(
        blocker
        for requirement in PRODUCTION_EVIDENCE_REQUIREMENTS
        if (blocker := requirement.validate(evidence)) is not None
    )
    report = ProductionReadinessReport(
        passed=not blockers,
        blockers=blockers,
        evidence_keys=tuple(requirement.key for requirement in PRODUCTION_EVIDENCE_REQUIREMENTS),
    )
    if audit_bus is not None:
        audit_bus.record(
            "production_readiness_evaluated",
            "deployment",
            {
                "passed": report.passed,
                "blockers": list(report.blockers),
                "evidence_keys": list(report.evidence_keys),
                "provided_keys": sorted(evidence.keys()),
            },
        )
    return report


def assert_production_readiness(
    evidence: Mapping[str, str],
    *,
    audit_bus: AuditBus | None = None,
) -> ProductionReadinessReport:
    """Fail closed unless every external production prerequisite is present."""

    report = evaluate_production_readiness(evidence, audit_bus=audit_bus)
    if not report.passed:
        raise ProductionReadinessError("; ".join(report.blockers))
    return report


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
        decision = (
            DeploymentDecision.COMPLETE if self.percentage == 100 else DeploymentDecision.ADVANCE
        )
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
