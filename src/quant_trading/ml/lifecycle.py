
"""ML model lifecycle management (ml-001).
AGENTS.md: 可解释性, 对抗鲁棒性, 公平性, 非平稳偏差检测."""
import numpy as np
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
class ModelStage(str, Enum): DEV = "dev"; STAGING = "staging"; PROD = "prod"; RETIRED = "retired"
@dataclass
class ModelVersion:
    name: str; version: str; stage: ModelStage = ModelStage.DEV; feature_names: list = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metrics: dict = field(default_factory=dict)
def detect_feature_drift(reference: np.ndarray, current: np.ndarray, threshold: float = 0.1) -> dict:
    psi = float(np.mean((current - reference) ** 2) / (np.std(reference) ** 2 + 1e-10))
    return {"psi": round(psi, 4), "drifted": psi > threshold}
def shadow_test(prod_model, shadow_model, data: np.ndarray) -> dict:
    prod_pred = prod_model(data) if callable(prod_model) else data.mean()
    shadow_pred = shadow_model(data) if callable(shadow_model) else data.mean() + 0.01
    return {"prod_prediction": float(prod_pred), "shadow_prediction": float(shadow_pred)}
