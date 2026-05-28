"""
Order management and exchange integration (exec-001).
AGENTS.md §7: Support limit/market/stop/take-profit orders.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import NoReturn

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


class OrderManager:
    """Submit orders only after system-state and configured market-time gates pass."""

    def __init__(
        self,
        audit_bus: AuditBus | None = None,
        system_fsm: SystemStateMachine | None = None,
        decision_clock: Callable[[], datetime] | None = None,
        reduce_only_validator: Callable[[Order], bool] | None = None,
    ) -> None:
        self._orders: dict[str, Order] = {}
        self._audit = audit_bus or AuditBus()
        self._system_fsm = system_fsm or SystemStateMachine()
        self._decision_clock = decision_clock or (lambda: datetime.now(UTC))
        self._reduce_only_validator = reduce_only_validator

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

        if order.client_order_id in self._orders:
            # Idempotency: same client_order_id returns existing order status.
            existing = self._orders[order.client_order_id]
            self._audit.record(
                "order_idempotent_replay",
                "order_manager",
                {
                    "client_order_id": order.client_order_id,
                    "symbol": existing.symbol,
                    "status": existing.status.value,
                },
            )
            return existing.client_order_id

        if order.market is None:
            self._reject_market_schedule(order, "order market is required for schedule enforcement")
        allowed, reason = MarketRouter.is_trading_allowed(
            order.market,
            self._decision_clock(),
        )

        if (
            not allowed
            and "settlement window" in reason
            and order.reduce_only
            and self._reduce_only_validator is not None
            and self._reduce_only_validator(order)
        ):
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
        self._orders[order.client_order_id] = order
        order.status = OrderStatus.SUBMITTED
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
            self._orders[order_id].status = OrderStatus.CANCELLED
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
