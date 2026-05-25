"""Capital allocation firewall for portfolio-003.

This module is intentionally conservative: reserved capital for pending
orders is treated as consumed until explicitly released or settled.
"""

from __future__ import annotations

from collections.abc import Mapping

from quant_trading.core.audit import AuditBus


class CapitalFirewall:
    """Prevent strategy allocations and reservations from exceeding limits."""

    def __init__(
        self,
        total_capital: float,
        max_per_strategy: float = 0.30,
        audit_bus: AuditBus | None = None,
    ) -> None:
        if total_capital <= 0:
            raise ValueError("total_capital must be positive")
        if not 0 < max_per_strategy <= 1:
            raise ValueError("max_per_strategy must be in (0, 1]")
        self.total = float(total_capital)
        self.max_pct = float(max_per_strategy)
        self.allocations: dict[str, float] = {}
        self.reservations: dict[str, float] = {}
        self.isolated_strategies: set[str] = set()
        self.audit = audit_bus or AuditBus()

    def _used_excluding(self, strategy_id: str) -> float:
        return (
            sum(self.allocations.values())
            + sum(self.reservations.values())
            - self.allocations.get(strategy_id, 0.0)
            - self.reservations.get(strategy_id, 0.0)
        )

    def _allowed(self, strategy_id: str, allocation: float, reservation: float) -> bool:
        combined = allocation + reservation
        return (
            strategy_id not in self.isolated_strategies
            and allocation >= 0
            and reservation >= 0
            and combined <= self.total * self.max_pct
            and self._used_excluding(strategy_id) + combined <= self.total
        )

    def allocate(self, strategy_id: str, amount: float) -> bool:
        """Set a strategy allocation rather than cumulatively double-count it."""

        reservation = self.reservations.get(strategy_id, 0.0)
        if not self._allowed(strategy_id, float(amount), reservation):
            self.audit.record(
                "capital_allocation_rejected",
                "capital_firewall",
                {"strategy_id": strategy_id, "amount": amount},
            )
            return False
        self.allocations[strategy_id] = float(amount)
        self.audit.record(
            "capital_allocated",
            "capital_firewall",
            {"strategy_id": strategy_id, "amount": amount, "reserved": reservation},
        )
        return True

    def reserve_pending(self, strategy_id: str, amount: float) -> bool:
        """Reserve cash for pending orders so they cannot be allocated twice."""

        allocation = self.allocations.get(strategy_id, 0.0)
        if not self._allowed(strategy_id, allocation, float(amount)):
            self.audit.record(
                "capital_reservation_rejected",
                "capital_firewall",
                {"strategy_id": strategy_id, "amount": amount},
            )
            return False
        self.reservations[strategy_id] = float(amount)
        self.audit.record(
            "capital_reserved",
            "capital_firewall",
            {"strategy_id": strategy_id, "amount": amount},
        )
        return True

    def release_pending(self, strategy_id: str) -> float:
        released = self.reservations.pop(strategy_id, 0.0)
        self.audit.record(
            "capital_reservation_released",
            "capital_firewall",
            {"strategy_id": strategy_id, "amount": released},
        )
        return released

    def release(self, strategy_id: str) -> float:
        released = self.allocations.pop(strategy_id, 0.0)
        self.reservations.pop(strategy_id, None)
        self.audit.record(
            "capital_released",
            "capital_firewall",
            {"strategy_id": strategy_id, "amount": released},
        )
        return released

    def available(self) -> float:
        return self.total - sum(self.allocations.values()) - sum(self.reservations.values())

    def isolate(self, strategy_id: str, reason: str) -> float:
        """Quarantine a strategy; subsequent allocations are rejected."""

        released = self.release(strategy_id)
        self.isolated_strategies.add(strategy_id)
        self.audit.record(
            "strategy_capital_isolated",
            "capital_firewall",
            {"strategy_id": strategy_id, "reason": reason, "released": released},
        )
        return released

    def adjust_for_var(self, var_limits: Mapping[str, float]) -> dict[str, float]:
        """Compress allocations to externally computed risk-contribution limits."""

        adjustments: dict[str, float] = {}
        for strategy_id, limit in var_limits.items():
            if not 0 <= limit <= 1:
                raise ValueError("VaR capital limit must be between 0 and 1")
            current = self.allocations.get(strategy_id)
            if current is None:
                continue
            max_allowed = float(limit) * self.total
            if current > max_allowed:
                adjustments[strategy_id] = current - max_allowed
                self.allocations[strategy_id] = max_allowed
                self.audit.record(
                    "capital_var_compressed",
                    "capital_firewall",
                    {
                        "strategy_id": strategy_id,
                        "released": adjustments[strategy_id],
                        "new_limit": max_allowed,
                    },
                )
        return adjustments
