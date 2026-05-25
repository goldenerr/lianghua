"""Database & state persistence (db-001). AGENTS.md: Event Sourcing + CQRS + multi-tenant isolation."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class StorageBackend(str, Enum):
    PARQUET = "parquet"
    CLICKHOUSE = "clickhouse"
    POSTGRES = "postgres"


@dataclass
class EventStore:
    backend: StorageBackend = StorageBackend.PARQUET
    events: list[dict] = field(default_factory=list)

    @staticmethod
    def _hash(event: dict) -> str:
        body = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def append(self, event_type: str, payload: dict, tenant_id: str = "default") -> int:
        event = {
            "sequence": len(self.events),
            "type": event_type,
            "payload": copy.deepcopy(payload),
            "tenant": tenant_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "previous_hash": self.events[-1]["hash"] if self.events else "GENESIS",
        }
        event["hash"] = self._hash(event)
        self.events.append(event)
        return len(self.events) - 1

    def replay(self, tenant_id: str | None = None) -> list[dict]:
        evts = (
            self.events
            if tenant_id is None
            else [e for e in self.events if e["tenant"] == tenant_id]
        )
        return copy.deepcopy(sorted(evts, key=lambda e: e["sequence"]))

    def verify_integrity(self) -> bool:
        previous_hash = "GENESIS"
        for sequence, event in enumerate(self.events):
            if event.get("sequence") != sequence or event.get("previous_hash") != previous_hash:
                return False
            body = dict(event)
            actual_hash = body.pop("hash", "")
            if self._hash(body) != actual_hash:
                return False
            previous_hash = actual_hash
        return True
