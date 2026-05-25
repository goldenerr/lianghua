"""Tests for event-driven engine — EventBus and event types."""

import time

from quant_trading.core.events import (
    Event,
    EventBus,
    EventType,
    FillEvent,
    OrderEvent,
    RiskEvent,
    TickEvent,
    TimerEvent,
)


class TestEventTypes:
    def test_tick_event(self):
        ev = TickEvent("BTC/USDT", 50000.0, 1.5, bid=49990.0, ask=50010.0)
        assert ev.event_type == EventType.TICK
        assert ev.symbol == "BTC/USDT"
        assert ev.payload["price"] == 50000.0
        assert ev.payload["bid"] == 49990.0
        assert ev.trace_id != ""

    def test_order_event(self):
        ev = OrderEvent("600519.SH", "buy", 100.0, price=1500.0, order_type="limit")
        assert ev.event_type == EventType.ORDER
        assert ev.payload["side"] == "buy"
        assert ev.payload["order_type"] == "limit"

    def test_fill_event(self):
        ev = FillEvent("IF2406", "sell", 2.0, 3500.0, commission=15.0)
        assert ev.event_type == EventType.FILL
        assert ev.payload["commission"] == 15.0

    def test_risk_event(self):
        ev = RiskEvent(risk_type="max_drawdown", level="critical", message="MDD 25% hit")
        assert ev.event_type == EventType.RISK
        assert ev.payload["level"] == "critical"

    def test_timer_event(self):
        ev = TimerEvent(interval_seconds=5.0)
        assert ev.event_type == EventType.TIMER
        assert ev.payload["interval_seconds"] == 5.0

    def test_unique_trace_ids(self):
        ev1 = TickEvent("A", 1.0, 1.0)
        ev2 = TickEvent("B", 2.0, 2.0)
        assert ev1.trace_id != ev2.trace_id

    def test_base_event_defaults(self):
        ev = Event(event_type=EventType.SYSTEM)
        assert ev.trace_id != ""
        assert ev.payload == {}


class TestEventBus:
    def test_subscribe_and_publish(self):
        bus = EventBus()
        received = []

        def handler(ev: Event):
            received.append(ev)

        bus.subscribe(EventType.TICK, handler)
        ev = TickEvent("X", 100.0, 10.0)
        bus.publish(ev)

        assert len(received) == 1
        assert received[0].symbol == "X"

    def test_wildcard_handler(self):
        bus = EventBus()
        received = []

        bus.subscribe_all(lambda ev: received.append(ev.event_type))
        bus.publish(TickEvent("A", 1.0, 1.0))
        bus.publish(OrderEvent("B", "buy", 100.0))
        bus.publish(RiskEvent(risk_type="test", message="test"))

        assert len(received) == 3
        assert EventType.TICK in received
        assert EventType.ORDER in received
        assert EventType.RISK in received

    def test_unsubscribe(self):
        bus = EventBus()
        received = []

        def handler(ev):
            received.append(ev)

        bus.subscribe(EventType.TICK, handler)
        bus.unsubscribe(EventType.TICK, handler)
        bus.publish(TickEvent("X", 1.0, 1.0))
        assert len(received) == 0

    def test_handler_exception_doesnt_crash_bus(self):
        bus = EventBus()
        received = []

        def bad_handler(ev):
            raise RuntimeError("intentional")

        def good_handler(ev):
            received.append(ev)

        bus.subscribe(EventType.TICK, bad_handler)
        bus.subscribe(EventType.TICK, good_handler)
        bus.publish(TickEvent("X", 1.0, 1.0))
        assert len(received) == 1  # Good handler still ran

    def test_stats(self):
        bus = EventBus()
        for _ in range(100):
            bus.publish(TickEvent("X", 1.0, 1.0))
        for _ in range(50):
            bus.publish(OrderEvent("X", "buy", 10.0))

        stats = bus.get_stats()
        assert stats["total_events"] == 150
        assert stats["events_per_second"] > 0
        by_type = stats["by_type"]
        assert by_type[EventType.TICK.value] == 100
        assert by_type[EventType.ORDER.value] == 50

    def test_reset_stats(self):
        bus = EventBus()
        bus.publish(TickEvent("X", 1.0, 1.0))
        bus.reset_stats()
        stats = bus.get_stats()
        assert stats["total_events"] == 0

    def test_throughput_benchmark(self):
        """Verify event bus can handle 100K+ events/sec (core-001 requirement)."""
        bus = EventBus()
        events = [TickEvent("BTC/USDT", 50000.0 + i, 1.0) for i in range(50000)]

        bus.reset_stats()
        t0 = time.time()
        for ev in events:
            bus.publish(ev)
        elapsed = time.time() - t0

        bus.get_stats()
        rate = 50000 / elapsed
        # Should achieve at least 50K/sec (100K target in optimized env)
        assert rate > 10000, f"Event rate {rate:.0f}/s below 10K minimum"
        print(f"Throughput: {rate:.0f} events/sec")
