"""
Global immutable audit event bus (core-003).
AGENTS.md §8: All important operations publish to this bus.
AGENTS.md §25: Event bus is the only trusted log source.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from .events import Event, EventBus, EventType

UTC = timezone.utc


class AuditBus:
    """Immutable audit trail for all system events."""

    def __init__(self, event_bus: EventBus | None = None) -> None:
        self.bus = event_bus or EventBus()
        self._events: list[dict[str, Any]] = []
        self._subscribed = False

    @staticmethod
    def _hash_entry(entry: dict[str, Any]) -> str:
        material = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def record(self, event_type: str, source: str, payload: dict[str, Any], **kwargs: Any) -> None:
        previous_hash = self._events[-1]["hash"] if self._events else "GENESIS"
        entry = {
            "sequence": len(self._events),
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": event_type,
            "source": source,
            "payload": payload,
            "trace_id": kwargs.get("trace_id", ""),
            "previous_hash": previous_hash,
        }
        entry["hash"] = self._hash_entry(entry)
        self._events.append(entry)
        ev = Event(event_type=EventType.SYSTEM, payload=entry)
        self.bus.publish(ev)

    def query(self, event_type: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        events = self._events
        if event_type:
            events = [e for e in events if e["event_type"] == event_type]
        return events[-limit:]

    def get_count(self) -> int:
        return len(self._events)

    def verify_integrity(self) -> bool:
        previous_hash = "GENESIS"
        for idx, event in enumerate(self._events):
            if event.get("sequence") != idx:
                return False
            if event.get("previous_hash") != previous_hash:
                return False
            expected = dict(event)
            actual_hash = expected.pop("hash", "")
            if self._hash_entry(expected) != actual_hash:
                return False
            previous_hash = actual_hash
        return True
