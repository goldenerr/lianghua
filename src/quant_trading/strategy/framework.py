"""
Pluggable strategy framework with version management, hot-reload, A/B testing.

AGENTS.md §2 (strategy-001): Strategy base class, plugin loading, factor library.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import pandas as pd

from quant_trading.config.market_router import MarketRouter
from quant_trading.config.settings import Market
from quant_trading.core.audit import AuditBus

UTC = timezone.utc


class SignalType(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass
class Signal:
    symbol: str
    signal_type: SignalType
    strength: float = 1.0
    price: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict = field(default_factory=dict)


class StrategyState(str, Enum):
    INIT = "init"
    WARMUP = "warmup"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"


@dataclass
class StrategyConfig:
    name: str = "strategy"
    version: str = "1.0.0"
    data_schema_version: str = "v2026.05"
    symbols: list[str] = field(default_factory=list)
    warmup_bars: int = 20
    market: Market | None = None
    parameters: dict[str, Any] = field(default_factory=dict)


class Strategy(ABC):
    """Abstract base for all strategies."""

    def __init__(
        self,
        config: StrategyConfig,
        audit_bus: AuditBus | None = None,
        decision_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.state = StrategyState.INIT
        self._bar_count = 0
        self._signals: list[Signal] = []
        self._orders: list[dict] = []
        self._fills: list[dict] = []
        self._risk_events: list[dict] = []
        self._audit = audit_bus or AuditBus()
        self._decision_clock = decision_clock or (lambda: datetime.now(UTC))

    @abstractmethod
    def on_data(self, data: dict[str, pd.DataFrame]) -> list[Signal]:
        """Process new data and generate signals."""

    def on_order(self, order: dict) -> None:
        self._orders.append({"timestamp": datetime.now(UTC), **order})

    def on_fill(self, fill: dict) -> None:
        self._fills.append({"timestamp": datetime.now(UTC), **fill})

    def on_risk(self, risk_event: dict) -> None:
        self._risk_events.append({"timestamp": datetime.now(UTC), **risk_event})
        level = str(risk_event.get("level", "")).lower()
        if level in {"critical", "emergency", "safe_mode"}:
            self.pause()

    def event_history(self) -> dict[str, list[dict]]:
        return {
            "orders": list(self._orders),
            "fills": list(self._fills),
            "risk_events": list(self._risk_events),
        }

    def warmup_complete(self) -> bool:
        return self._bar_count >= self.config.warmup_bars

    def start(self) -> None:
        self.state = StrategyState.WARMUP

    def stop(self) -> None:
        self.state = StrategyState.STOPPED

    def pause(self) -> None:
        self.state = StrategyState.PAUSED

    def resume(self) -> None:
        self.state = StrategyState.RUNNING

    def _signals_allowed_by_market_schedule(self) -> bool:
        if self.config.market is None:
            reason = "strategy market is required for schedule enforcement"
            self._record_signal_suppression(reason)
            return False
        try:
            allowed, reason = MarketRouter.is_trading_allowed(
                self.config.market,
                self._decision_clock(),
            )
        except ValueError as exc:
            allowed, reason = False, str(exc)
        if not allowed:
            self._record_signal_suppression(reason)
        return allowed

    def _record_signal_suppression(self, reason: str) -> None:
        self._audit.record(
            "strategy_signal_suppressed_market_schedule",
            self.config.name,
            {
                "market": self.config.market.value if self.config.market else None,
                "reason": reason,
            },
        )


class MovingAverageCrossStrategy(Strategy):
    """Dual moving average crossover."""

    def __init__(
        self,
        config: StrategyConfig,
        audit_bus: AuditBus | None = None,
        decision_clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(config, audit_bus=audit_bus, decision_clock=decision_clock)
        self.fast = config.parameters.get("fast", 20)
        self.slow = config.parameters.get("slow", 50)

    def on_data(self, data: dict[str, pd.DataFrame]) -> list[Signal]:
        self._bar_count += 1
        if not self._signals_allowed_by_market_schedule():
            return []
        signals = []
        for sym, df in data.items():
            if len(df) < self.slow:
                continue
            fast_ma = df["close"].rolling(self.fast).mean().iloc[-1]
            slow_ma = df["close"].rolling(self.slow).mean().iloc[-1]
            prev_fast = (
                df["close"].rolling(self.fast).mean().iloc[-2]
                if len(df) > self.fast + 1
                else fast_ma
            )
            prev_slow = (
                df["close"].rolling(self.slow).mean().iloc[-2]
                if len(df) > self.slow + 1
                else slow_ma
            )
            if prev_fast <= prev_slow and fast_ma > slow_ma:
                signals.append(Signal(sym, SignalType.BUY, price=float(df["close"].iloc[-1])))
            elif prev_fast >= prev_slow and fast_ma < slow_ma:
                signals.append(Signal(sym, SignalType.SELL, price=float(df["close"].iloc[-1])))
        return signals


class RSIStrategy(Strategy):
    """RSI mean reversion."""

    def on_data(self, data: dict[str, pd.DataFrame]) -> list[Signal]:
        self._bar_count += 1
        if not self._signals_allowed_by_market_schedule():
            return []
        signals = []
        period = self.config.parameters.get("period", 14)
        oversold = self.config.parameters.get("oversold", 30)
        overbought = self.config.parameters.get("overbought", 70)
        for sym, df in data.items():
            if len(df) < period + 1:
                continue
            delta = df["close"].diff()
            gain = delta.clip(lower=0).rolling(period).mean().iloc[-1]
            loss = (-delta.clip(upper=0)).rolling(period).mean().iloc[-1]
            rs = gain / max(loss, 1e-10)
            rsi = 100 - 100 / (1 + rs)
            close = float(df["close"].iloc[-1])
            if rsi < oversold:
                signals.append(Signal(sym, SignalType.BUY, price=close))
            elif rsi > overbought:
                signals.append(Signal(sym, SignalType.SELL, price=close))
        return signals
