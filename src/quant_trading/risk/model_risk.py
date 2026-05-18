
"""Model risk management (risk-005). AGENTS.md: drift monitoring, shadow testing."""
from dataclasses import dataclass
@dataclass
class DriftReport:
    psi: float = 0.0; feature_drift: dict = None; alert: bool = False
