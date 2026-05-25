"""
Order management and exchange integration (exec-001).
AGENTS.md §7: Support limit/market/stop/take-profit orders.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from quant_trading.core.audit import AuditBus
from quant_trading.core.state_machine import SystemStateMachine

UTC = timezone.utc

class OrderSide(str, Enum): BUY = "buy"; SELL = "sell"
class OrderType(str, Enum): MARKET = "market"; LIMIT = "limit"; STOP = "stop"; TAKE_PROFIT = "take_profit"
class OrderStatus(str, Enum): PENDING = "pending"; SUBMITTED = "submitted"; PARTIAL = "partial"; FILLED = "filled"; CANCELLED = "cancelled"

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

class OrderManager:
    def __init__(
        self,
        audit_bus: AuditBus | None = None,
        system_fsm: SystemStateMachine | None = None,
    ):
        self._orders: dict[str, Order] = {}
        self._audit = audit_bus or AuditBus()
        self._system_fsm = system_fsm or SystemStateMachine()

    def submit(self, order: Order) -> str:
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
            },
        )
        return order.client_order_id

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

    def get(self, order_id: str) -> Order | None: return self._orders.get(order_id)
    def get_active(self) -> list[Order]: return [o for o in self._orders.values() if o.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PARTIAL)]
