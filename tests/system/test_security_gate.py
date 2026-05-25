from datetime import datetime, timedelta, timezone

import pytest
from quant_trading.core.audit import AuditBus
from quant_trading.security.auth import ApiKey, AuditTrail, Role, User
from quant_trading.security.signing import sign_code, verify_code

UTC = timezone.utc


def test_rbac_enforces_separation_of_duties() -> None:
    assert User("ops", Role.OPERATIONS).can("deploy") is True
    assert User("trader", Role.TRADER).can("risk_configure") is False
    assert User("risk", Role.RISK).can("approve") is True
    assert User("auditor", Role.AUDITOR).can("trade") is False


def test_api_key_lifetime_usage_and_deactivation_are_audited() -> None:
    now = datetime(2026, 5, 25, tzinfo=UTC)
    audit = AuditBus()
    key = ApiKey("binance-paper-1", "paper-alpha", now + timedelta(days=60), now)
    key.record_use("127.0.0.1", audit, now + timedelta(days=1))
    assert key.use_count == 1
    assert key.needs_rotation(now + timedelta(days=54)) is True
    assert audit.query("api_key_use")[0]["payload"]["key_id"] == key.key_id

    key.deactivate("suspected compromise", audit)
    with pytest.raises(PermissionError):
        key.record_use("127.0.0.1", audit, now + timedelta(days=2))
    assert audit.query("api_key_deactivated")


def test_api_key_rejects_excessive_lifetime() -> None:
    now = datetime(2026, 5, 25, tzinfo=UTC)
    with pytest.raises(ValueError, match="90"):
        ApiKey("too-long", "strategy", now + timedelta(days=91), now)


def test_security_audit_facade_uses_hash_chained_bus() -> None:
    trail = AuditTrail()
    trail.log("risk", "approve", "risk.yaml")
    assert len(trail.query(user="risk", action="approve")) == 1
    assert trail.audit_bus.verify_integrity() is True


def test_code_signature_requires_secret_and_detects_tampering() -> None:
    signature = sign_code("strategy source", b"release-secret", "vault-key-1")
    assert verify_code("strategy source", signature, b"release-secret") is True
    assert verify_code("modified source", signature, b"release-secret") is False
    assert verify_code("strategy source", signature, b"wrong-secret") is False
    with pytest.raises(ValueError, match="signing_key"):
        sign_code("strategy source", b"", "vault-key-1")
