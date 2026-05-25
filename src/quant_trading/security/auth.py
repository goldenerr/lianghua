"""Security primitives for RBAC and API-key usage auditing (security-001).

Secret issuance and production rotation remain responsibilities of Vault or an
approved secret manager; this module stores metadata only, never raw secrets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

from quant_trading.core.audit import AuditBus

UTC = timezone.utc


class Role(str, Enum):
    OPERATIONS = "operations"
    TRADER = "trader"
    RISK = "risk"
    AUDITOR = "auditor"
    ADMIN = "admin"


ROLE_PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.OPERATIONS: frozenset({"deploy", "monitor", "restart"}),
    Role.TRADER: frozenset({"trade", "strategy_control"}),
    Role.RISK: frozenset({"risk_configure", "kill_switch", "approve"}),
    Role.AUDITOR: frozenset({"audit"}),
    Role.ADMIN: frozenset(
        {
            "deploy",
            "monitor",
            "restart",
            "trade",
            "strategy_control",
            "risk_configure",
            "kill_switch",
            "approve",
            "audit",
        }
    ),
}


@dataclass(frozen=True)
class User:
    username: str
    role: Role

    def can(self, action: str) -> bool:
        """Check least-privilege role permissions."""
        return action in ROLE_PERMISSIONS[self.role]


@dataclass
class ApiKey:
    """Non-secret key metadata with expiry and usage tracking."""

    key_id: str
    subject: str
    expires_at: datetime
    last_rotated: datetime
    active: bool = True
    use_count: int = 0
    last_used_at: str | None = None
    last_ip: str | None = None

    def __post_init__(self) -> None:
        if not self.key_id or not self.subject:
            raise ValueError("key_id and subject are required")
        if self.expires_at.tzinfo is None or self.last_rotated.tzinfo is None:
            raise ValueError("API key timestamps must be timezone-aware")
        if self.expires_at <= self.last_rotated:
            raise ValueError("API key expiry must be after rotation time")
        if self.expires_at - self.last_rotated > timedelta(days=90):
            raise ValueError("API key lifetime must not exceed 90 days")

    def needs_rotation(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        return self.active and current >= self.expires_at - timedelta(days=7)

    def record_use(self, ip_address: str, audit_bus: AuditBus, now: datetime | None = None) -> None:
        current = now or datetime.now(UTC)
        if not self.active or current >= self.expires_at:
            raise PermissionError("inactive or expired API key cannot be used")
        self.use_count += 1
        self.last_used_at = current.isoformat()
        self.last_ip = ip_address
        audit_bus.record(
            "api_key_use",
            self.subject,
            {"key_id": self.key_id, "ip_address": ip_address, "use_count": self.use_count},
        )

    def deactivate(self, reason: str, audit_bus: AuditBus) -> None:
        if not reason.strip():
            raise ValueError("deactivation reason is required")
        self.active = False
        audit_bus.record(
            "api_key_deactivated", self.subject, {"key_id": self.key_id, "reason": reason}
        )


@dataclass
class AuditTrail:
    """Compatibility facade that records security actions on the authoritative bus."""

    audit_bus: AuditBus = field(default_factory=AuditBus)

    def log(self, user: str, action: str, resource: str) -> None:
        self.audit_bus.record("security_operation", user, {"action": action, "resource": resource})

    def query(self, user: str | None = None, action: str | None = None) -> list[dict]:
        records = self.audit_bus.query("security_operation")
        if user is not None:
            records = [entry for entry in records if entry["source"] == user]
        if action is not None:
            records = [entry for entry in records if entry["payload"]["action"] == action]
        return records
