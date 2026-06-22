"""
Global immutable audit event bus (core-003).
AGENTS.md §8: All important operations publish to this bus.
AGENTS.md §25: Event bus is the only trusted log source.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .events import Event, EventBus, EventType

UTC = timezone.utc
_REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "api_secret",
        "secret",
        "password",
        "passphrase",
        "token",
        "access_token",
        "private_key",
    }
)


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


class SecretProvider(Protocol):
    """Minimal secret-provider contract for development/test archive signing."""

    def get_secret(self, ref: str) -> str:
        """Return secret material for a non-production signing reference."""


class EnvLocalSecretProvider:
    """Read test/dev secrets from `.env.local` without hard-coding them in code."""

    def __init__(self, path: str | Path = ".env.local") -> None:
        self.path = Path(path)

    def get_secret(self, ref: str) -> str:
        if not ref.startswith("env-local://"):
            raise ValueError("EnvLocalSecretProvider only supports env-local:// references")
        key = ref.removeprefix("env-local://").strip()
        if not key:
            raise ValueError("env-local secret reference must include a key")
        values = self._read_values()
        secret = values.get(key, "").strip()
        if not secret:
            raise ValueError(f"missing secret in .env.local for key: {key}")
        return secret

    def _read_values(self) -> dict[str, str]:
        if not self.path.is_file():
            raise FileNotFoundError(f".env.local file not found: {self.path}")
        values: dict[str, str] = {}
        for raw_line in self.path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
        return values


class InMemorySecretManager:
    """In-memory test secret manager for deterministic unit tests."""

    def __init__(self, secrets: Mapping[str, str]) -> None:
        self._secrets = dict(secrets)

    def get_secret(self, ref: str) -> str:
        secret = self._secrets.get(ref, "").strip()
        if not secret:
            raise ValueError(f"missing test secret for reference: {ref}")
        return secret


@dataclass(frozen=True)
class AuditArchiveResult:
    """Result of one daily audit archive operation."""

    archive_date: date
    log_path: Path
    manifest_path: Path
    event_count: int
    log_sha256: str
    manifest_hash: str
    attestation_ref: str


class LocalAuditWormArchive:
    """Development/test substitute for external WORM using daily files and hashes.

    This adapter intentionally uses externalized signing material from
    `.env.local` or a test secret manager. It provides audit evidence for local
    gates, but it is not a replacement for approved production WORM storage.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        secret_provider: SecretProvider | None = None,
        key_ref: str = "env-local://AUDIT_ARCHIVE_HMAC_KEY",
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.secret_provider = secret_provider or EnvLocalSecretProvider()
        self.key_ref = key_ref

    def archive_day(
        self, audit_bus: AuditBus, archive_date: date | None = None
    ) -> AuditArchiveResult:
        target_date = archive_date or datetime.now(UTC).date()
        events = [
            _redact_value(event)
            for event in audit_bus.query(limit=1_000_000)
            if datetime.fromisoformat(event["timestamp"]).date() == target_date
        ]
        day_root = self.root / target_date.isoformat()
        day_root.mkdir(parents=True, exist_ok=True)
        log_path = day_root / "audit.jsonl"
        manifest_path = day_root / "audit-manifest.json"
        if log_path.exists() or manifest_path.exists():
            raise FileExistsError("daily audit archive is write-once and already exists")

        lines = [_canonical_json(event) for event in events]
        log_bytes = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
        with log_path.open("xb") as stream:
            stream.write(log_bytes)
        log_hash = hashlib.sha256(log_bytes).hexdigest()
        unsigned_manifest: dict[str, Any] = {
            "archive_date": target_date.isoformat(),
            "event_count": len(events),
            "log_file": log_path.name,
            "log_sha256": log_hash,
            "algorithm": "hmac-sha256",
            "key_ref": self.key_ref,
            "generated_at": datetime.now(UTC).isoformat(),
        }
        manifest_hash = hashlib.sha256(_canonical_json(unsigned_manifest).encode()).hexdigest()
        signature = hmac.new(
            self.secret_provider.get_secret(self.key_ref).encode("utf-8"),
            _canonical_json(unsigned_manifest).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        manifest = {
            **unsigned_manifest,
            "manifest_hash": manifest_hash,
            "signature": signature,
        }
        with manifest_path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2) + "\n")
        audit_bus.record(
            "audit_log_archived",
            "local_audit_worm_archive",
            {
                "archive_date": target_date.isoformat(),
                "event_count": len(events),
                "log_sha256": log_hash,
                "manifest_hash": manifest_hash,
            },
        )
        return AuditArchiveResult(
            archive_date=target_date,
            log_path=log_path,
            manifest_path=manifest_path,
            event_count=len(events),
            log_sha256=log_hash,
            manifest_hash=manifest_hash,
            attestation_ref=f"worm://audit-file/{target_date.isoformat()}/{manifest_hash}",
        )

    def verify_day(self, archive_date: date) -> bool:
        day_root = self.root / archive_date.isoformat()
        log_path = day_root / "audit.jsonl"
        manifest_path = day_root / "audit-manifest.json"
        if not log_path.is_file() or not manifest_path.is_file():
            return False
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        unsigned = {
            key: manifest[key]
            for key in (
                "archive_date",
                "event_count",
                "log_file",
                "log_sha256",
                "algorithm",
                "key_ref",
                "generated_at",
            )
        }
        if hashlib.sha256(log_path.read_bytes()).hexdigest() != manifest["log_sha256"]:
            return False
        expected_hash = hashlib.sha256(_canonical_json(unsigned).encode()).hexdigest()
        if expected_hash != manifest.get("manifest_hash"):
            return False
        expected_signature = hmac.new(
            self.secret_provider.get_secret(str(manifest["key_ref"])).encode("utf-8"),
            _canonical_json(unsigned).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected_signature, str(manifest.get("signature", "")))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, nested in value.items():
            key_text = str(key)
            if key_text.lower() in _SENSITIVE_KEYS:
                redacted[key_text] = _REDACTED
            else:
                redacted[key_text] = _redact_value(nested)
        return redacted
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value
