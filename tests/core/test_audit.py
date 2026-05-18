"""Tests for audit bus (core-003)."""
import pytest
from quant_trading.core.audit import AuditBus
from quant_trading.core.events import Event, EventType, EventBus


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
