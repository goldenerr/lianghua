"""
Order management and exchange integration (exec-001).
AGENTS.md §7: Support limit/market/stop/take-profit orders.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

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
    price: Optional[float] = None
    stop_price: Optional[float] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    avg_price: float = 0.0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    timeout_seconds: int = 30

class OrderManager:
    def __init__(self): self._orders: dict[str, Order] = {}
    def submit(self, order: Order) -> str:
        self._orders[order.client_order_id] = order
        order.status = OrderStatus.SUBMITTED
        return order.client_order_id
    def cancel(self, order_id: str) -> bool:
        if order_id in self._orders:
            self._orders[order_id].status = OrderStatus.CANCELLED
            return True
        return False
    def get(self, order_id: str) -> Optional[Order]: return self._orders.get(order_id)
    def get_active(self) -> list[Order]: return [o for o in self._orders.values() if o.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PARTIAL)]
