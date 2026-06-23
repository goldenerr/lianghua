"""Tests for audit bus (core-003)."""

import json
from datetime import date, datetime, timezone

import pytest
from quant_trading.core.audit import (
    AuditBus,
    EnvLocalSecretProvider,
    InMemorySecretManager,
    LocalAuditWormArchive,
)
from quant_trading.core.events import EventBus, EventType


class TestAuditBus:
    @pytest.fixture
    def bus(self):
        return AuditBus()

    def test_record(self, bus):
        bus.record("order_submitted", "strategy_1", {"symbol": "AAPL", "qty": 10})
        assert bus.get_count() == 1

    def test_record_with_trace_id(self, bus):
        bus.record("risk_check", "risk_engine", {"var": 0.03}, trace_id="trace-123")
        events = bus.query()
        assert len(events) == 1
        assert events[0]["trace_id"] == "trace-123"

    def test_record_multiple(self, bus):
        for i in range(5):
            bus.record("tick", "data_feed", {"price": 100 + i})
        assert bus.get_count() == 5

    def test_query_all(self, bus):
        bus.record("order", "strat", {"qty": 10})
        bus.record("fill", "exec", {"qty": 10})
        results = bus.query()
        assert len(results) == 2

    def test_query_by_type(self, bus):
        bus.record("order", "strat", {"qty": 10})
        bus.record("fill", "exec", {"qty": 5})
        bus.record("order", "strat", {"qty": 20})
        orders = bus.query(event_type="order")
        assert len(orders) == 2
        fills = bus.query(event_type="fill")
        assert len(fills) == 1

    def test_query_limit(self, bus):
        for i in range(10):
            bus.record("event", "src", {"i": i})
        results = bus.query(limit=3)
        assert len(results) == 3
        # should return last 3
        assert results[-1]["payload"]["i"] == 9

    def test_query_nonexistent_type(self, bus):
        bus.record("test", "src", {})
        results = bus.query(event_type="no_such_type")
        assert len(results) == 0

    def test_event_published_to_bus(self):
        eb = EventBus()
        received = []

        def handler(ev):
            received.append(ev)

        eb.subscribe(EventType.SYSTEM, handler)
        audit = AuditBus(event_bus=eb)
        audit.record("test_event", "test_source", {"data": 1})
        assert len(received) == 1

    def test_timestamp_format(self, bus):
        bus.record("event", "src", {})
        ts = bus.query()[0]["timestamp"]
        assert "T" in ts  # ISO format

    def test_hash_chain_integrity(self, bus):
        bus.record("order", "exec", {"id": "o1"})
        bus.record("fill", "exec", {"id": "o1", "qty": 10})
        events = bus.query(limit=10)
        assert events[0]["previous_hash"] == "GENESIS"
        assert events[1]["previous_hash"] == events[0]["hash"]
        assert bus.verify_integrity() is True

    def test_tamper_detection(self, bus):
        bus.record("risk_check", "risk", {"passed": True})
        bus.query()[0]["payload"]["passed"] = False
        assert bus.verify_integrity() is False


def test_local_audit_worm_archive_writes_daily_redacted_log_and_hash(tmp_path) -> None:
    archive_date = datetime.now(timezone.utc).date()
    audit = AuditBus()
    audit.record(
        "api_call",
        "exchange",
        {
            "symbol": "600519.SH",
            "api_key": "raw-key",
            "nested": {"api_secret": "raw-secret", "safe": "visible"},
        },
    )
    archive = LocalAuditWormArchive(
        tmp_path,
        secret_provider=InMemorySecretManager({"test-sm://audit-key": "signing-secret"}),
        key_ref="test-sm://audit-key",
    )

    result = archive.archive_day(audit, archive_date)

    assert result.event_count == 1
    assert result.attestation_ref.startswith(f"worm://audit-file/{archive_date.isoformat()}/")
    assert archive.verify_day(archive_date) is True
    assert "raw-key" not in result.log_path.read_text(encoding="utf-8")
    assert "raw-secret" not in result.log_path.read_text(encoding="utf-8")
    assert "[REDACTED]" in result.log_path.read_text(encoding="utf-8")
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["log_sha256"] == result.log_sha256
    assert manifest["manifest_hash"] == result.manifest_hash
    assert manifest["key_ref"] == "test-sm://audit-key"
    with pytest.raises(FileExistsError, match="write-once"):
        archive.archive_day(audit, archive_date)


def test_local_audit_worm_archive_detects_tampering(tmp_path) -> None:
    audit = AuditBus()
    audit.record("order", "strategy", {"symbol": "AAPL", "qty": 1})
    archive = LocalAuditWormArchive(
        tmp_path,
        secret_provider=InMemorySecretManager({"test-sm://audit-key": "signing-secret"}),
        key_ref="test-sm://audit-key",
    )
    result = archive.archive_day(audit, date(2026, 5, 29))

    result.log_path.write_text('{"tampered":true}\n', encoding="utf-8")

    assert archive.verify_day(date(2026, 5, 29)) is False


def test_env_local_secret_provider_loads_key_without_hardcoding(tmp_path) -> None:
    env_path = tmp_path / ".env.local"
    env_path.write_text(
        "# dev/test only\nAUDIT_ARCHIVE_HMAC_KEY='from-env-local'\n",
        encoding="utf-8",
    )

    provider = EnvLocalSecretProvider(env_path)

    assert provider.get_secret("env-local://AUDIT_ARCHIVE_HMAC_KEY") == "from-env-local"
