"""
Order management and exchange integration (exec-001).
AGENTS.md §7: Support limit/market/stop/take-profit orders.
"""

from __future__ import annotations

import copy
import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import NoReturn, Protocol

from quant_trading.config.market_router import MarketRouter, MarketRuleSet
from quant_trading.config.settings import Market
from quant_trading.core.audit import AuditBus
from quant_trading.core.state_machine import SystemStateMachine

UTC = timezone.utc


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    TAKE_PROFIT = "take_profit"


class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"


@dataclass
class Order:
    client_order_id: str
    symbol: str
    side: OrderSide
    quantity: float
    order_type: OrderType = OrderType.MARKET
    price: float | None = None
    stop_price: float | None = None
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    avg_price: float = 0.0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    timeout_seconds: int = 30
    market: Market | None = None
    reduce_only: bool = False


class OrderStore(Protocol):
    """Durable source of truth for idempotent order intents."""

    def load_all(self) -> list[Order]: ...

    def get(self, client_order_id: str) -> Order | None: ...

    def reserve(self, order: Order) -> bool: ...

    def update_status(self, order: Order) -> None: ...


class InMemoryOrderStore:
    """Non-durable store for unit tests only."""

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}

    def load_all(self) -> list[Order]:
        return copy.deepcopy(list(self._orders.values()))

    def get(self, client_order_id: str) -> Order | None:
        order = self._orders.get(client_order_id)
        return copy.deepcopy(order) if order else None

    def reserve(self, order: Order) -> bool:
        if order.client_order_id in self._orders:
            return False
        self._orders[order.client_order_id] = copy.deepcopy(order)
        return True

    def update_status(self, order: Order) -> None:
        if order.client_order_id not in self._orders:
            raise KeyError(f"Unknown order: {order.client_order_id}")
        self._orders[order.client_order_id] = copy.deepcopy(order)


class SqliteOrderStore:
    """SQLite-backed order intent store with a durable unique id constraint."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS order_intents (
                    client_order_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    order_type TEXT NOT NULL,
                    price REAL,
                    stop_price REAL,
                    status TEXT NOT NULL,
                    filled_qty REAL NOT NULL,
                    avg_price REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    timeout_seconds INTEGER NOT NULL,
                    market TEXT,
                    reduce_only INTEGER NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @staticmethod
    def _values(order: Order) -> tuple[object, ...]:
        return (
            order.client_order_id,
            order.symbol,
            order.side.value,
            order.quantity,
            order.order_type.value,
            order.price,
            order.stop_price,
            order.status.value,
            order.filled_qty,
            order.avg_price,
            order.created_at.isoformat(),
            order.timeout_seconds,
            order.market.value if order.market else None,
            int(order.reduce_only),
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Order:
        return Order(
            client_order_id=row["client_order_id"],
            symbol=row["symbol"],
            side=OrderSide(row["side"]),
            quantity=row["quantity"],
            order_type=OrderType(row["order_type"]),
            price=row["price"],
            stop_price=row["stop_price"],
            status=OrderStatus(row["status"]),
            filled_qty=row["filled_qty"],
            avg_price=row["avg_price"],
            created_at=datetime.fromisoformat(row["created_at"]),
            timeout_seconds=row["timeout_seconds"],
            market=Market(row["market"]) if row["market"] else None,
            reduce_only=bool(row["reduce_only"]),
        )

    def load_all(self) -> list[Order]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM order_intents ORDER BY created_at, client_order_id"
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def get(self, client_order_id: str) -> Order | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM order_intents WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        return self._from_row(row) if row else None

    def reserve(self, order: Order) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO order_intents VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._values(order),
            )
        return cursor.rowcount == 1

    def update_status(self, order: Order) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE order_intents
                SET status = ?, filled_qty = ?, avg_price = ?
                WHERE client_order_id = ?
                """,
                (
                    order.status.value,
                    order.filled_qty,
                    order.avg_price,
                    order.client_order_id,
                ),
            )
        if cursor.rowcount != 1:
            raise KeyError(f"Unknown order: {order.client_order_id}")


@dataclass(frozen=True)
class ReconciledPositionSnapshot:
    """Fresh exchange-reconciled position used to prove a reduce-only order."""

    quantity: float
    as_of: datetime


class ReconciledReduceOnlyValidator:
    """Validate reduce-only intent against a fresh reconciled position snapshot."""

    def __init__(
        self,
        position_provider: Callable[[Order], ReconciledPositionSnapshot],
        *,
        max_age_seconds: int = 30,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        self._position_provider = position_provider
        self._max_age = timedelta(seconds=max_age_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))

    def __call__(self, order: Order) -> bool:
        snapshot = self._position_provider(order)
        if snapshot.as_of.tzinfo is None or snapshot.as_of.utcoffset() is None:
            raise ValueError("reconciled position timestamp must be timezone-aware")
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("reduce-only validation clock must be timezone-aware")
        if now.astimezone(UTC) - snapshot.as_of.astimezone(UTC) > self._max_age:
            raise ValueError("reconciled position snapshot is stale")
        if not math.isfinite(snapshot.quantity):
            raise ValueError("reconciled position quantity must be finite")
        if snapshot.quantity == 0:
            return False
        if order.quantity > abs(snapshot.quantity):
            return False
        if snapshot.quantity > 0:
            return order.side == OrderSide.SELL
        return order.side == OrderSide.BUY


class OrderManager:
    """Submit orders only after system-state and configured market-time gates pass."""

    def __init__(
        self,
        audit_bus: AuditBus | None = None,
        system_fsm: SystemStateMachine | None = None,
        decision_clock: Callable[[], datetime] | None = None,
        reduce_only_validator: Callable[[Order], bool] | None = None,
        order_store: OrderStore | None = None,
    ) -> None:
        if order_store is None:
            raise ValueError("order_store is required for durable idempotency")
        self._order_store = order_store
        self._orders = {order.client_order_id: order for order in order_store.load_all()}
        self._audit = audit_bus or AuditBus()
        self._system_fsm = system_fsm or SystemStateMachine()
        self._decision_clock = decision_clock or (lambda: datetime.now(UTC))
        self._reduce_only_validator = reduce_only_validator

    @staticmethod
    def _intent_fingerprint(order: Order) -> tuple[object, ...]:
        return (
            order.symbol,
            order.side,
            order.quantity,
            order.order_type,
            order.price,
            order.stop_price,
            order.timeout_seconds,
            order.market,
            order.reduce_only,
        )

    def _handle_existing(self, requested: Order, existing: Order) -> str:
        if self._intent_fingerprint(requested) != self._intent_fingerprint(existing):
            self._audit.record(
                "order_idempotency_conflict",
                "order_manager",
                {
                    "client_order_id": requested.client_order_id,
                    "requested_symbol": requested.symbol,
                    "persisted_symbol": existing.symbol,
                },
            )
            raise RuntimeError(
                f"Order submission blocked: idempotency conflict for {requested.client_order_id}"
            )
        self._orders[existing.client_order_id] = existing
        self._audit.record(
            "order_idempotent_replay",
            "order_manager",
            {
                "client_order_id": requested.client_order_id,
                "symbol": existing.symbol,
                "status": existing.status.value,
            },
        )
        return existing.client_order_id

    def submit(self, order: Order) -> str:
        """Submit a new order or replay an existing idempotent request safely."""
        if not self._system_fsm.can_trade:
            self._audit.record(
                "order_rejected_system_state",
                "order_manager",
                {
                    "client_order_id": order.client_order_id,
                    "symbol": order.symbol,
                    "system_state": self._system_fsm.state.value,
                },
            )
            raise RuntimeError(
                f"Order submission blocked: system state is {self._system_fsm.state.value}"
            )

        existing = self._order_store.get(order.client_order_id)
        if existing is not None:
            return self._handle_existing(order, existing)

        if order.market is None:
            self._reject_market_schedule(order, "order market is required for schedule enforcement")
        allowed, reason = MarketRouter.is_trading_allowed(
            order.market,
            self._decision_clock(),
        )

        if not allowed and "settlement window" in reason and order.reduce_only:
            self._validate_reduce_only_settlement_order(order)
            allowed, reason = MarketRouter.is_trading_allowed(
                order.market,
                self._decision_clock(),
                allow_settlement_window=True,
            )
            self._audit.record(
                "order_reduce_only_settlement_approved",
                "order_manager",
                {
                    "client_order_id": order.client_order_id,
                    "symbol": order.symbol,
                    "market": order.market.value,
                },
            )

        if not allowed:
            self._reject_market_schedule(order, reason)

        self._validate_market_rules(order)
        order.status = OrderStatus.PENDING
        if not self._order_store.reserve(order):
            existing = self._order_store.get(order.client_order_id)
            if existing is None:
                raise RuntimeError("Order store uniqueness conflict without persisted order")
            return self._handle_existing(order, existing)

        order.status = OrderStatus.SUBMITTED
        try:
            self._order_store.update_status(order)
        except Exception:
            order.status = OrderStatus.PENDING
            raise
        self._orders[order.client_order_id] = order
        self._audit.record(
            "order_submitted",
            "order_manager",
            {
                "client_order_id": order.client_order_id,
                "symbol": order.symbol,
                "side": order.side.value,
                "quantity": order.quantity,
                "order_type": order.order_type.value,
                "market": order.market.value,
                "reduce_only": order.reduce_only,
            },
        )
        return order.client_order_id

    def _validate_reduce_only_settlement_order(self, order: Order) -> None:
        if self._reduce_only_validator is None:
            self._reject_reduce_only(order, "validator_missing")
        try:
            verified = self._reduce_only_validator(order)
        except Exception as exc:
            self._audit.record(
                "order_reduce_only_settlement_rejected",
                "order_manager",
                {
                    "client_order_id": order.client_order_id,
                    "symbol": order.symbol,
                    "market": order.market.value if order.market else None,
                    "reason": "validator_error",
                    "error_type": type(exc).__name__,
                },
            )
            raise RuntimeError("Order submission blocked: reduce-only validator failed") from exc
        if not verified:
            self._reject_reduce_only(order, "not_reducing_reconciled_position")

    def _reject_reduce_only(self, order: Order, reason: str) -> NoReturn:
        self._audit.record(
            "order_reduce_only_settlement_rejected",
            "order_manager",
            {
                "client_order_id": order.client_order_id,
                "symbol": order.symbol,
                "market": order.market.value if order.market else None,
                "reason": reason,
            },
        )
        raise RuntimeError(f"Order submission blocked: reduce-only settlement validation {reason}")

    def _validate_market_rules(self, order: Order) -> None:
        """Reject orders that do not comply with configured lot and tick rules."""
        assert order.market is not None
        rules = MarketRouter.get_rules(order.market)
        if not math.isfinite(order.quantity) or order.quantity <= 0:
            self._reject_market_rule(order, "quantity must be finite and positive")
        if rules.round_quantity(order.quantity) != order.quantity:
            self._reject_market_rule(
                order, f"quantity must be a multiple of lot size {rules.lot_size}"
            )

        if order.order_type == OrderType.MARKET:
            if order.price is not None or order.stop_price is not None:
                self._reject_market_rule(order, "market order must not contain price fields")
            return

        if order.order_type == OrderType.LIMIT:
            if order.price is None or order.stop_price is not None:
                self._reject_market_rule(order, "limit order requires price only")
            self._validate_tick_price(order, order.price, rules, "price")
            return

        if order.stop_price is None or order.price is not None:
            self._reject_market_rule(order, "stop or take-profit order requires stop_price only")
        self._validate_tick_price(order, order.stop_price, rules, "stop_price")

    def _validate_tick_price(
        self, order: Order, price: float, rules: MarketRuleSet, field_name: str
    ) -> None:
        try:
            rounded = rules.round_price(price)
        except ValueError as exc:
            self._reject_market_rule(order, f"{field_name}: {exc}")
        if not math.isclose(price, rounded, rel_tol=0.0, abs_tol=rules.tick_size * 1e-9):
            self._reject_market_rule(
                order, f"{field_name} must align to tick size {rules.tick_size}"
            )

    def _reject_market_schedule(self, order: Order, reason: str) -> NoReturn:
        self._audit.record(
            "order_rejected_market_schedule",
            "order_manager",
            {
                "client_order_id": order.client_order_id,
                "symbol": order.symbol,
                "market": order.market.value if order.market else None,
                "reduce_only": order.reduce_only,
                "reason": reason,
            },
        )
        raise RuntimeError(f"Order submission blocked by market schedule: {reason}")

    def _reject_market_rule(self, order: Order, reason: str) -> NoReturn:
        self._audit.record(
            "order_rejected_market_rule",
            "order_manager",
            {
                "client_order_id": order.client_order_id,
                "symbol": order.symbol,
                "market": order.market.value if order.market else None,
                "reason": reason,
            },
        )
        raise RuntimeError(f"Order submission blocked by market rule: {reason}")

    def cancel(self, order_id: str) -> bool:
        if order_id in self._orders:
            order = self._orders[order_id]
            previous_status = order.status
            order.status = OrderStatus.CANCELLED
            try:
                self._order_store.update_status(order)
            except Exception:
                order.status = previous_status
                raise
            self._audit.record(
                "order_cancelled",
                "order_manager",
                {"client_order_id": order_id},
            )
            return True
        self._audit.record(
            "order_cancel_failed",
            "order_manager",
            {"client_order_id": order_id, "reason": "not_found"},
        )
        return False

    def get(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def get_active(self) -> list[Order]:
        return [
            o
            for o in self._orders.values()
            if o.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PARTIAL)
        ]
