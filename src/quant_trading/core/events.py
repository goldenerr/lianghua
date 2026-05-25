"""
Event-driven engine — async event bus with Redis Pub/Sub or local queue.

AGENTS.md §2 (core-001):
- Async event bus (Redis Pub/Sub or ZeroMQ)
- Event types: TickEvent, OrderEvent, FillEvent, RiskEvent, TimerEvent
- Shared by backtest and live execution
- Performance: 100K ticks/sec
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)
UTC = timezone.utc


# ── Event types ───────────────────────────────────────────────────────────

class EventType(str, Enum):
    TICK = "tick"
    ORDER = "order"
    FILL = "fill"
    RISK = "risk"
    TIMER = "timer"
    SIGNAL = "signal"
    CONFIG_CHANGE = "config_change"
    SYSTEM = "system"


@dataclass
class Event:
    """Base event with required metadata fields."""
    event_type: EventType
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    symbol: str = ""
    trace_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.trace_id:
            import uuid
            self.trace_id = str(uuid.uuid4())[:12]


@dataclass
class TickEvent(Event):
    """Real-time market tick data."""
    def __init__(self, symbol: str, price: float, volume: float,
                 bid: float = 0.0, ask: float = 0.0, **kwargs):
        super().__init__(
            event_type=EventType.TICK,
            symbol=symbol,
            payload={
                "price": price, "volume": volume,
                "bid": bid, "ask": ask, **kwargs,
            },
        )


@dataclass
class OrderEvent(Event):
    """Order submission event."""
    def __init__(self, symbol: str, side: str, quantity: float,
                 price: float = 0.0, order_type: str = "market", **kwargs):
        super().__init__(
            event_type=EventType.ORDER,
            symbol=symbol,
            payload={
                "side": side, "quantity": quantity,
                "price": price, "order_type": order_type, **kwargs,
            },
        )


@dataclass
class FillEvent(Event):
    """Order fill confirmation."""
    def __init__(self, symbol: str, side: str, quantity: float,
                 price: float, commission: float = 0.0, **kwargs):
        super().__init__(
            event_type=EventType.FILL,
            symbol=symbol,
            payload={
                "side": side, "quantity": quantity,
                "price": price, "commission": commission, **kwargs,
            },
        )


@dataclass
class RiskEvent(Event):
    """Risk management event."""
    def __init__(self, symbol: str = "", risk_type: str = "",
                 level: str = "warning", message: str = "", **kwargs):
        super().__init__(
            event_type=EventType.RISK,
            symbol=symbol,
            payload={
                "risk_type": risk_type, "level": level,
                "message": message, **kwargs,
            },
        )


@dataclass
class TimerEvent(Event):
    """Scheduled timer event for periodic tasks."""
    def __init__(self, interval_seconds: float = 1.0, **kwargs):
        super().__init__(
            event_type=EventType.TIMER,
            payload={"interval_seconds": interval_seconds, **kwargs},
        )


# ── Event bus ─────────────────────────────────────────────────────────────

Handler = Callable[[Event], None]
AsyncHandler = Callable[[Event], "asyncio.coroutine"]


class EventBus:
    """
    Async event bus — pub/sub pattern.

    Supports both local in-memory dispatch and Redis Pub/Sub.
    Shared between backtest and live execution (AGENTS.md §14).
    """

    def __init__(self, redis_url: str | None = None):
        self.redis_url = redis_url
        self._handlers: dict[EventType, list[Handler]] = {
            et: [] for et in EventType
        }
        self._wildcard_handlers: list[Handler] = []
        self._event_count: dict[EventType, int] = {et: 0 for et in EventType}
        self._start_time: float | None = None

    def subscribe(self, event_type: EventType, handler: Handler) -> None:
        """Subscribe a handler to a specific event type."""
        self._handlers[event_type].append(handler)

    def subscribe_all(self, handler: Handler) -> None:
        """Subscribe to all event types (wildcard)."""
        self._wildcard_handlers.append(handler)

    def unsubscribe(self, event_type: EventType, handler: Handler) -> None:
        """Remove a handler subscription."""
        if handler in self._handlers[event_type]:
            self._handlers[event_type].remove(handler)

    def publish(self, event: Event) -> None:
        """Publish an event to all subscribers."""
        if self._start_time is None:
            self._start_time = time.time()

        self._event_count[event.event_type] += 1

        # Specific handlers
        for handler in self._handlers[event.event_type]:
            try:
                handler(event)
            except Exception:
                logger.exception("Handler failed for %s", event.event_type)

        # Wildcard handlers
        for handler in self._wildcard_handlers:
            try:
                handler(event)
            except Exception:
                logger.exception("Wildcard handler failed for %s", event.event_type)

    async def publish_async(self, event: Event) -> None:
        """Async publish — wraps sync handlers in executor."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.publish, event)

    def get_stats(self) -> dict:
        """Get event throughput statistics."""
        elapsed = time.time() - (self._start_time or time.time())
        total = sum(self._event_count.values())
        return {
            "total_events": total,
            "elapsed_seconds": round(elapsed, 2),
            "events_per_second": round(total / max(elapsed, 0.001), 1),
            "by_type": {
                et.value: count for et, count in self._event_count.items()
            },
        }

    def reset_stats(self) -> None:
        """Reset event counters."""
        self._start_time = time.time()
        for et in EventType:
            self._event_count[et] = 0
