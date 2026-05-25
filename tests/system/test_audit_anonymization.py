import pytest
from quant_trading.core.audit import AuditBus
from quant_trading.security.anonymizer import AuditQueryService, anonymize, query_audit_log
from quant_trading.security.auth import Role, User


def test_anonymizer_masks_secrets_and_personal_identifiers() -> None:
    text = "api_key=abc123 password=hunter2 Bearer token123 user@example.com 13800138000"
    masked = anonymize(text)
    assert "abc123" not in masked
    assert "hunter2" not in masked
    assert "token123" not in masked
    assert "user@example.com" not in masked
    assert "13800138000" not in masked


def test_audit_query_reads_actual_bus_and_recursively_redacts_payloads() -> None:
    bus = AuditBus()
    bus.record(
        "order_access",
        "strategy-a",
        {"strategy_id": "alpha", "api_key": "raw-key", "details": {"email": "risk@example.com"}},
    )
    service = AuditQueryService(bus)
    auditor = User("auditor", Role.AUDITOR)

    records = service.query(auditor, {"strategy_id": "alpha"})
    assert len(records) == 1
    assert records[0]["payload"]["api_key"] == "[REDACTED]"
    assert "risk@example.com" not in records[0]["payload"]["details"]["email"]


def test_audit_query_and_export_fail_closed_without_authorization() -> None:
    bus = AuditBus()
    bus.record("trade", "trader", {"symbol": "BTC"})
    service = AuditQueryService(bus)
    with pytest.raises(PermissionError):
        service.query(User("trader", Role.TRADER))
    with pytest.raises(PermissionError):
        query_audit_log({}, bus, User("trader", Role.TRADER))
    with pytest.raises(PermissionError, match="reference"):
        service.export_csv(User("risk-auditor", Role.AUDITOR), "")


def test_approved_export_is_sanitized_and_audited() -> None:
    bus = AuditBus()
    bus.record("key_use", "engine", {"token": "sensitive-token"})
    service = AuditQueryService(bus)

    output = service.export_csv(User("auditor", Role.AUDITOR), "APR-SEC-001")

    assert "sensitive-token" not in output
    assert "[REDACTED]" in output
    assert bus.query("audit_exported")
    assert bus.verify_integrity()
