
"""Online learning & model drift detection (ml-002). AGENTS.md: incremental updates, concept drift."""
import numpy as np
def detect_concept_drift(old_data: np.ndarray, new_data: np.ndarray, window: int=50) -> dict:
    old_mean, new_mean = np.mean(old_data[-window:]), np.mean(new_data[-window:])
    drift = abs(new_mean-old_mean)/(np.std(old_data)+1e-10)
    return {"drift_score": round(float(drift), 4), "significant": drift > 0.5}
