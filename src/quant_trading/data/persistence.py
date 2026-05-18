
"""Database & state persistence (db-001). AGENTS.md: Event Sourcing + CQRS + multi-tenant isolation."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
class StorageBackend(str, Enum): PARQUET = "parquet"; CLICKHOUSE = "clickhouse"; POSTGRES = "postgres"
@dataclass
class EventStore:
    backend: StorageBackend = StorageBackend.PARQUET; events: list = field(default_factory=list)
    def append(self, event_type: str, payload: dict, tenant_id: str = "default") -> int:
        self.events.append({"type": event_type, "payload": payload, "tenant": tenant_id,
                            "ts": datetime.now(timezone.utc).isoformat()})
        return len(self.events) - 1
    def replay(self, tenant_id: str = None) -> list:
        evts = self.events if tenant_id is None else [e for e in self.events if e["tenant"] == tenant_id]
        return sorted(evts, key=lambda e: e["ts"])
