"""Position reconciler (exec-002). AGENTS.md §18: 30s reconciliation, Safe Mode on mismatch."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from quant_trading.core.audit import AuditBus
from quant_trading.core.state_machine import SystemStateMachine


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
        system_fsm: SystemStateMachine,
        tolerance: float = 0.0001,
        audit_bus: AuditBus | None = None,
    ):
        self.tolerance = tolerance
        self._audit = audit_bus
        self._system_fsm = system_fsm

    def _compare(self, internal: float, exchange: float, *, enter_safe_mode: bool) -> bool:
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
        if not matched and enter_safe_mode:
            self._enter_safe_mode(internal, exchange, diff)
        return matched

    def _enter_safe_mode(self, internal: float, exchange: float, diff: float) -> None:
        self._system_fsm.enter_safe_mode(
            f"Position mismatch: internal={internal}, exchange={exchange}, diff={diff:.6f}"
        )

    def reconcile(self, internal: float, exchange: float) -> bool:
        return self._compare(internal, exchange, enter_safe_mode=True)

    async def reconcile_with_retry(
        self,
        fetch_internal: Callable[[], Awaitable[float]],
        fetch_exchange: Callable[[], Awaitable[float]],
        max_retries: int = 5,
        backoff_seconds: tuple[float, ...] = (1, 2, 4, 8, 16),
    ) -> ReconcileResult:
        """Asynchronous reconcile; enter Safe Mode only after all retries fail."""
        last_internal = 0.0
        last_exchange = 0.0
        last_diff = 0.0
        attempts = 0
        total_attempts = min(max_retries, len(backoff_seconds))

        try:
            for idx in range(total_attempts):
                attempts = idx + 1
                last_internal = await fetch_internal()
                last_exchange = await fetch_exchange()
                last_diff = abs(last_internal - last_exchange) / max(abs(last_exchange), 1.0)
                if self._compare(last_internal, last_exchange, enter_safe_mode=False):
                    return ReconcileResult(
                        matched=True,
                        internal=last_internal,
                        exchange=last_exchange,
                        relative_diff=last_diff,
                        attempts=attempts,
                    )
                if idx < total_attempts - 1:
                    await asyncio.sleep(backoff_seconds[idx])
        except Exception as exc:
            if self._audit:
                self._audit.record(
                    "position_reconcile_error",
                    "reconciler",
                    {"error_type": type(exc).__name__, "attempts": attempts},
                )
            self._system_fsm.enter_safe_mode(f"Position reconciliation error: {type(exc).__name__}")
            raise

        self._enter_safe_mode(last_internal, last_exchange, last_diff)
        return ReconcileResult(
            matched=False,
            internal=last_internal,
            exchange=last_exchange,
            relative_diff=last_diff,
            attempts=attempts,
        )
