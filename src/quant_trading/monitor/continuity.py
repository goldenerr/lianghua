
"""Business continuity & fault injection platform (monitor-003)."""
from dataclasses import dataclass


@dataclass
class FaultInjector:
    target: str
    fault_type: str = "latency"
    active: bool = False
    last_duration_ms: int = 0

    def inject(self, duration_ms: int=100) -> dict:
        if duration_ms < 0:
            raise ValueError("duration_ms must be non-negative")
        self.active = True
        self.last_duration_ms = duration_ms
        return {
            "target": self.target,
            "fault": self.fault_type,
            "duration_ms": duration_ms,
            "injected": True,
        }

    def recover(self) -> dict:
        was_active = self.active
        self.active = False
        return {"target": self.target, "fault": self.fault_type, "recovered": was_active}
