"""
System monitoring (monitor-001).
AGENTS.md §8: Structured logging, alert channels.
"""

from datetime import timezone

UTC = timezone.utc


class SystemMonitor:
    def __init__(self) -> None:
        self._metrics: dict[str, float] = {
            "cpu_pct": 0.0,
            "mem_pct": 0.0,
            "active_orders": 0.0,
            "latency_ms": 0.0,
        }

    def update(self, **kwargs: float) -> None:
        self._metrics.update(kwargs)

    def get(self) -> dict[str, float]:
        return dict(self._metrics)

    def is_healthy(self) -> bool:
        return self._metrics["cpu_pct"] < 80 and self._metrics["mem_pct"] < 80
