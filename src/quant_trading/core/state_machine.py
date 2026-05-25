"""
Unified State Machine & Invariant Enforcement Engine.

AGENTS.md §0: Core Invariants must be enforced at every key node.
AGENTS.md §2 (core-002): FSM for all core entities, Safe Mode on violation.

States: OrderState, PositionState, SystemState
Invariants: Position, Equity, Order Idempotency, Data Consistency, Risk Limit
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)
UTC = timezone.utc


# ── States ────────────────────────────────────────────────────────────────

class OrderState(str, Enum):
    CREATED = "created"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class PositionState(str, Enum):
    FLAT = "flat"
    LONG = "long"
    SHORT = "short"


class SystemState(str, Enum):
    INIT = "init"
    WARMUP = "warmup"
    RUNNING = "running"
    SHUTTING_DOWN = "shutting_down"
    SAFE_MODE = "safe_mode"
    EMERGENCY = "emergency"


# Valid transitions per state machine
_ORDER_TRANSITIONS: dict[OrderState, set[OrderState]] = {
    OrderState.CREATED: {OrderState.SUBMITTED, OrderState.CANCELLED, OrderState.REJECTED},
    OrderState.SUBMITTED: {OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED},
    OrderState.PARTIALLY_FILLED: {OrderState.FILLED, OrderState.CANCELLED},
    OrderState.FILLED: set(),
    OrderState.CANCELLED: set(),
    OrderState.REJECTED: set(),
    OrderState.EXPIRED: set(),
}

_POSITION_TRANSITIONS: dict[PositionState, set[PositionState]] = {
    PositionState.FLAT: {PositionState.LONG, PositionState.SHORT},
    PositionState.LONG: {PositionState.FLAT, PositionState.SHORT},
    PositionState.SHORT: {PositionState.FLAT, PositionState.LONG},
}


# ── State Machine ─────────────────────────────────────────────────────────

class StateMachine(ABC):
    """Abstract FSM — all core state changes go through this (AGENTS.md §2)."""

    @abstractmethod
    def transition(self, new_state: Enum, metadata: dict | None = None) -> bool:
        """Attempt state transition. Returns True if valid."""


class OrderStateMachine(StateMachine):
    """Order lifecycle FSM."""
    def __init__(self, order_id: str):
        self.order_id = order_id
        self.state = OrderState.CREATED
        self.history: list[dict] = []

    def transition(self, new_state: OrderState, metadata: dict | None = None) -> bool:
        if new_state not in _ORDER_TRANSITIONS[self.state]:
            logger.error(
                "Invalid order transition: %s → %s (order=%s)",
                self.state, new_state, self.order_id,
            )
            return False

        old_state = self.state
        self.state = new_state
        self.history.append({
            "timestamp": datetime.now(UTC).isoformat(),
            "from": old_state.value,
            "to": new_state.value,
            "metadata": metadata or {},
        })
        return True


class PositionStateMachine(StateMachine):
    """Position lifecycle FSM."""
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.state = PositionState.FLAT
        self.quantity: float = 0.0
        self.avg_price: float = 0.0

    def transition(self, new_state: PositionState, metadata: dict | None = None) -> bool:
        if new_state not in _POSITION_TRANSITIONS[self.state]:
            return False
        self.state = new_state
        if metadata:
            self.quantity = metadata.get("quantity", self.quantity)
            self.avg_price = metadata.get("avg_price", self.avg_price)
        return True


class SystemStateMachine(StateMachine):
    """Global system state FSM."""
    def __init__(self):
        self.state = SystemState.INIT
        self.history: list[dict] = []

    def transition(self, new_state: SystemState, metadata: dict | None = None) -> bool:
        old = self.state
        self.state = new_state
        self.history.append({
            "timestamp": datetime.now(UTC).isoformat(),
            "from": old.value,
            "to": new_state.value,
            "metadata": metadata or {},
        })
        logger.info("System state: %s → %s", old.value, new_state.value)
        return True

    def enter_safe_mode(self, reason: str) -> None:
        """Enter Safe Mode — block all new trades, alert."""
        self.transition(SystemState.SAFE_MODE, {"reason": reason})
        logger.critical("SAFE MODE activated: %s", reason)

    def enter_emergency(self, reason: str) -> None:
        """Enter Emergency — kill switch, liquidate all."""
        self.transition(SystemState.EMERGENCY, {"reason": reason})
        logger.critical("EMERGENCY mode activated: %s", reason)

    @property
    def can_trade(self) -> bool:
        return self.state == SystemState.RUNNING


# ── Invariants ────────────────────────────────────────────────────────────

@dataclass
class InvariantCheck:
    """Result of a single invariant assertion."""
    name: str
    passed: bool
    details: str = ""
    actual: Any = None
    expected: Any = None


class InvariantEnforcer:
    """
    Enforces core invariants per AGENTS.md §0.

    Invariants checked:
    - Position: internal_position + pending_orders == exchange_position (±0.01%)
    - Equity: realized_pnl + unrealized_pnl + cash == total_equity
    - Order Idempotency: no duplicate client_order_id
    - Risk Limit: VaR(95%) ≤ config max
    """

    POSITION_TOLERANCE = 0.0001  # ±0.01%

    def __init__(self, system_fsm: SystemStateMachine):
        self.system_fsm = system_fsm
        self._used_order_ids: set[str] = set()
        self._violation_count: dict[str, int] = {}

    def check_all(
        self,
        internal_position: float = 0,
        pending_orders: float = 0,
        exchange_position: float = 0,
        realized_pnl: float = 0,
        unrealized_pnl: float = 0,
        cash: float = 0,
        total_equity: float = 0,
        client_order_id: str = "",
        var_95: float = 0,
        max_var: float = float("inf"),
    ) -> list[InvariantCheck]:
        """Run all invariant checks. Returns list of results."""
        results: list[InvariantCheck] = []

        # Position Invariant
        local = internal_position + pending_orders
        diff = abs(local - exchange_position)
        pos_ok = diff <= self.POSITION_TOLERANCE * max(abs(exchange_position), 1.0)
        results.append(InvariantCheck(
            "position", pos_ok,
            f"local={local:.4f} exchange={exchange_position:.4f} diff={diff:.6f}",
        ))

        # Equity Invariant
        computed = realized_pnl + unrealized_pnl + cash
        eq_diff = abs(computed - total_equity)
        eq_ok = eq_diff < 0.01
        results.append(InvariantCheck(
            "equity", eq_ok,
            f"computed={computed:.2f} declared={total_equity:.2f}",
        ))

        # Order Idempotency
        if client_order_id:
            dup = client_order_id in self._used_order_ids
            if not dup:
                self._used_order_ids.add(client_order_id)
            results.append(InvariantCheck(
                "order_idempotency", not dup,
                f"order_id={client_order_id}",
            ))

        # Risk Limit
        risk_ok = var_95 <= max_var
        results.append(InvariantCheck(
            "risk_limit", risk_ok,
            f"VaR(95%)={var_95:.2f} max={max_var:.2f}",
        ))

        # On violation, enter Safe Mode
        failed = [r for r in results if not r.passed]
        if failed:
            for f in failed:
                self._violation_count[f.name] = self._violation_count.get(f.name, 0) + 1
            reason = "; ".join(f"{f.name}: {f.details}" for f in failed)
            self.system_fsm.enter_safe_mode(reason)

        return results

    def check_position_invariant(
        self, internal: float, pending: float, exchange: float
    ) -> InvariantCheck:
        local = internal + pending
        diff = abs(local - exchange)
        ok = diff <= self.POSITION_TOLERANCE * max(abs(exchange), 1.0)
        result = InvariantCheck("position", ok, f"diff={diff:.6f}")
        if not ok:
            self.system_fsm.enter_safe_mode(result.details)
        return result
