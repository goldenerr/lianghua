"""
Global immutable audit event bus (core-003).
AGENTS.md §8: All important operations publish to this bus.
AGENTS.md §25: Event bus is the only trusted log source.
"""
from datetime import datetime, timezone
from typing import Any, Optional
from .events import Event, EventType, EventBus

UTC = timezone.utc

class AuditBus:
    """Immutable audit trail for all system events."""
    def __init__(self, event_bus: Optional[EventBus] = None):
        self.bus = event_bus or EventBus()
        self._events: list[dict] = []
        self._subscribed = False

    def record(self, event_type: str, source: str, payload: dict, **kwargs) -> None:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": event_type,
            "source": source,
            "payload": payload,
            "trace_id": kwargs.get("trace_id", ""),
        }
        self._events.append(entry)
        ev = Event(event_type=EventType.SYSTEM, payload=entry)
        self.bus.publish(ev)

    def query(self, event_type: Optional[str] = None, limit: int = 100) -> list[dict]:
        events = self._events
        if event_type:
            events = [e for e in events if e["event_type"] == event_type]
        return events[-limit:]

    def get_count(self) -> int:
        return len(self._events)
