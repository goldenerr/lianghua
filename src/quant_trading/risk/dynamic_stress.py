
"""Dynamic stress test generator (risk-004). AGENTS.md: regime-based scenario generation."""
import numpy as np
def generate_scenario(vol: float, days: int = 20, seed: int = 42) -> np.ndarray:
    rng = np.random.RandomState(seed)
    return rng.normal(-vol/2, vol, days)
