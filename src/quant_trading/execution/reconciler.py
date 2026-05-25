
"""Position reconciler (exec-002). AGENTS.md §18: 30s reconciliation, Safe Mode on mismatch."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from quant_trading.core.audit import AuditBus


@dataclass
class ReconcileResult:
    matched: bool
    internal: float
    exchange: float
    relative_diff: float
    attempts: int


class PositionReconciler:
    def __init__(
        self,
        tolerance: float = 0.0001,
        audit_bus: AuditBus | None = None,
        on_safe_mode: Callable[[str], None] | None = None,
    ):
        self.tolerance = tolerance
        self._audit = audit_bus
        self._on_safe_mode = on_safe_mode

    def reconcile(self, internal: float, exchange: float) -> bool:
        diff = abs(internal - exchange) / max(abs(exchange), 1.0)
        matched = diff <= self.tolerance
        if self._audit:
            self._audit.record(
                "position_reconcile",
                "reconciler",
                {
                    "internal": internal,
                    "exchange": exchange,
                    "relative_diff": diff,
                    "tolerance": self.tolerance,
                    "matched": matched,
                },
            )
        if not matched and self._on_safe_mode:
            self._on_safe_mode(
                f"Position mismatch: internal={internal}, exchange={exchange}, diff={diff:.6f}"
            )
        return matched

    async def reconcile_with_retry(
        self,
        fetch_internal: Callable[[], Awaitable[float]],
        fetch_exchange: Callable[[], Awaitable[float]],
        max_retries: int = 5,
        backoff_seconds: tuple[float, ...] = (1, 2, 4, 8, 16),
    ) -> ReconcileResult:
        """
        Asynchronous reconcile with exponential backoff.
        """
        last_internal = 0.0
        last_exchange = 0.0
        last_diff = 0.0
        attempts = 0
        total_attempts = min(max_retries, len(backoff_seconds))

        for idx in range(total_attempts):
            attempts = idx + 1
            last_internal = await fetch_internal()
            last_exchange = await fetch_exchange()
            last_diff = abs(last_internal - last_exchange) / max(abs(last_exchange), 1.0)
            if self.reconcile(last_internal, last_exchange):
                return ReconcileResult(
                    matched=True,
                    internal=last_internal,
                    exchange=last_exchange,
                    relative_diff=last_diff,
                    attempts=attempts,
                )
            if idx < total_attempts - 1:
                await asyncio.sleep(backoff_seconds[idx])

        return ReconcileResult(
            matched=False,
            internal=last_internal,
            exchange=last_exchange,
            relative_diff=last_diff,
            attempts=attempts,
        )
