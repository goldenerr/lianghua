"""Model risk management (risk-005). AGENTS.md: drift monitoring, shadow testing."""

from dataclasses import dataclass, field


@dataclass
class DriftReport:
    psi: float = 0.0
    feature_drift: dict[str, float] = field(default_factory=dict)
    alert: bool = False
