"""PII/secret masking and authorized audit-query utilities for security-002."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from typing import Any

from quant_trading.core.audit import AuditBus
from quant_trading.security.auth import User

_MASK_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1[REDACTED]"),
    (
        re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\b(\s*[:=]\s*)[^\s,;\"'}]+"),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"\b1[3-9]\d{9}\b"), "[PHONE_REDACTED]"),
    (re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"), "[EMAIL_REDACTED]"),
    (re.compile(r"\b\d{17}[\dXx]\b"), "[ID_REDACTED]"),
)
_SENSITIVE_KEYS = frozenset({"api_key", "apikey", "password", "secret", "token", "access_token"})


def anonymize(text: str) -> str:
    """Mask credential and personally identifiable substrings in text."""

    result = text
    for pattern, replacement in _MASK_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def anonymize_value(value: Any) -> Any:
    """Recursively sanitize structured audit event content."""

    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, nested in value.items():
            if str(key).lower() in _SENSITIVE_KEYS:
                sanitized[str(key)] = "[REDACTED]"
            else:
                sanitized[str(key)] = anonymize_value(nested)
        return sanitized
    if isinstance(value, list):
        return [anonymize_value(item) for item in value]
    if isinstance(value, str):
        return anonymize(value)
    return value


def hash_token(token: str) -> str:
    """Produce a correlation-safe one-way token identifier."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


class AuditQueryService:
    """Read-only RBAC-gated facade over the authoritative audit bus."""

    def __init__(self, audit_bus: AuditBus) -> None:
        self.audit_bus = audit_bus

    @staticmethod
    def _authorize(requester: User) -> None:
        if not requester.can("audit"):
            raise PermissionError("audit permission is required")

    def query(self, requester: User, filters: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
        self._authorize(requester)
        criteria = filters or {}
        events = deepcopy(self.audit_bus.query(limit=10000))
        if criteria.get("event_type"):
            events = [event for event in events if event["event_type"] == criteria["event_type"]]
        if criteria.get("source"):
            events = [event for event in events if event["source"] == criteria["source"]]
        if criteria.get("strategy_id"):
            events = [
                event
                for event in events
                if event.get("payload", {}).get("strategy_id") == criteria["strategy_id"]
            ]
        if criteria.get("from_timestamp"):
            start = datetime.fromisoformat(criteria["from_timestamp"])
            events = [event for event in events if datetime.fromisoformat(event["timestamp"]) >= start]
        return [anonymize_value(event) for event in events]

    def export_csv(
        self,
        requester: User,
        approval_ref: str,
        filters: Mapping[str, str] | None = None,
    ) -> str:
        """Export sanitized records only when an approval reference is supplied."""

        self._authorize(requester)
        if not approval_ref.strip():
            raise PermissionError("approved audit export reference is required")
        records = self.query(requester, filters)
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=["timestamp", "event_type", "source", "payload"])
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "timestamp": record["timestamp"],
                    "event_type": record["event_type"],
                    "source": record["source"],
                    "payload": json.dumps(record["payload"], sort_keys=True),
                }
            )
        self.audit_bus.record(
            "audit_exported",
            requester.username,
            {"approval_ref": approval_ref, "record_count": len(records)},
        )
        return stream.getvalue()


def query_audit_log(
    filters: Mapping[str, str],
    audit_bus: AuditBus | None = None,
    requester: User | None = None,
) -> list[dict[str, Any]]:
    """Compatibility API that fails closed unless an authorized source is supplied."""

    if audit_bus is None or requester is None:
        raise PermissionError("audit bus and authorized requester are required")
    return AuditQueryService(audit_bus).query(requester, filters)
