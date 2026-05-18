
"""Security & compliance (security-001). AGENTS.md: zero-trust, mTLS, RBAC, API key rotation, leak recovery."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
class Role(str, Enum): ADMIN = "admin"; TRADER = "trader"; RISK = "risk"; AUDITOR = "auditor"
@dataclass
class User: username: str; role: Role; permissions: set = None
    def can(self, action: str) -> bool:
        perm_map = {Role.ADMIN: {"trade", "configure", "audit", "kill_switch"},
                    Role.TRADER: {"trade"}, Role.RISK: {"configure", "kill_switch"},
                    Role.AUDITOR: {"audit"}}
        return action in perm_map.get(self.role, set())
@dataclass
class ApiKey: key_id: str; expires_at: datetime; last_rotated: datetime; active: bool = True
    def needs_rotation(self) -> bool: return datetime.now(timezone.utc) > self.expires_at - timedelta(days=7)
@dataclass
class AuditTrail:
    entries: list = None; def __post_init__(self): self.entries = self.entries or []
    def log(self, user: str, action: str, resource: str) -> None:
        self.entries.append({"ts": datetime.now(timezone.utc).isoformat(), "user": user, "action": action, "resource": resource})
    def query(self, user: str = None, action: str = None) -> list:
        result = self.entries
        if user: result = [e for e in result if e["user"] == user]
        if action: result = [e for e in result if e["action"] == action]
        return result
