"""Model risk management (risk-005). AGENTS.md: drift monitoring, shadow testing, emergency fallback."""

from dataclasses import dataclass
from enum import Enum

import numpy as np


class ModelHealth(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"


@dataclass
class ModelSentinel:
    psi_threshold: float = 0.2
    rolling_window: int = 20

    def check(self, preds: np.ndarray, actuals: np.ndarray) -> ModelHealth:
        err = np.mean((preds - actuals) ** 2)
        return ModelHealth.CRITICAL if err > self.psi_threshold else ModelHealth.HEALTHY

    def fallback(self) -> str:
        return "switching_to_rule_based_strategy"
