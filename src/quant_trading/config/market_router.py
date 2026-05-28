"""
Market adapter routing & futures contract rollover.

AGENTS.md §2 (config-002):
- Data sources select by primary_markets
- Risk rules vary by market type
- Fee/tax models switch by market
- Futures: approval-gated rollover intent detection on dominant contract switch
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone
from typing import ClassVar

from quant_trading.core.audit import AuditBus

from .market_calendar import CalendarRegistry, MarketCalendar
from .settings import (
    Environment,
    FuturesRolloverPolicy,
    Market,
    MarketRules,
    QuantSettings,
    SystemSettings,
)

UTC = timezone.utc
_ROLLOVER_CONFIGURATION_TOKEN = object()
_RUNTIME_PUBLICATION_TOKEN = object()


# ── Market rules (validated from config) ──────────────────────────────────────


class MarketRuleSet:
    """Per-market trading rules loaded from config/system.yaml -> market_rules."""

    def __init__(
        self,
        market: Market,
        tick_size: float = 0.01,
        lot_size: int = 100,
        price_precision: int = 2,
        data_sources: tuple[str, ...] = (),
        fee_model: str = "",
        funding_rate: float | None = None,
        settlement_time_utc: str | None = None,
    ):
        self.market = market
        self.tick_size = tick_size
        self.lot_size = lot_size
        self.price_precision = price_precision
        self.data_sources = data_sources
        self.fee_model = fee_model
        self.funding_rate = funding_rate
        self.settlement_time_utc = settlement_time_utc

    @classmethod
    def from_config(cls, configured: MarketRules) -> MarketRuleSet:
        return cls(
            market=configured.market,
            tick_size=configured.tick_size,
            lot_size=configured.lot_size,
            price_precision=configured.price_precision,
            data_sources=tuple(configured.data_sources),
            fee_model=configured.fee_model,
            funding_rate=configured.funding_rate,
            settlement_time_utc=configured.settlement_time,
        )

    def round_price(self, price: float) -> float:
        """Round a valid positive price to the nearest market tick size."""
        if not math.isfinite(price) or price <= 0:
            raise ValueError("price must be finite and positive")
        ticks = round(price / self.tick_size)
        rounded = ticks * self.tick_size
        if rounded <= 0:
            raise ValueError("price is below the minimum executable tick")
        return rounded

    def round_quantity(self, qty: float) -> float:
        """Normalize quantity toward zero so sizing never increases exposure."""
        if not math.isfinite(qty):
            raise ValueError("quantity must be finite")
        return math.trunc(qty / self.lot_size) * self.lot_size


# ── Futures contract rollover ─────────────────────────────────────────────────


class ContractMonth:
    """Futures contract month identifier (e.g., IF2406 = IF + 2024-06)."""

    def __init__(self, symbol: str):
        # Parse "IF2406" -> underlying="IF", year=2024, month=6.
        parsed = re.fullmatch(r"([A-Za-z][A-Za-z0-9]*)(\d{2})(\d{2})", symbol)
        if parsed is None:
            raise ValueError(f"invalid futures contract code: {symbol}")
        underlying, year, month = parsed.groups()
        parsed_month = int(month)
        if not 1 <= parsed_month <= 12:
            raise ValueError(f"invalid futures contract month: {symbol}")
        self.underlying = underlying
        self.year = int("20" + year)
        self.month = parsed_month
        self.code = symbol

    @property
    def expiry_date(self) -> date:
        """Estimated expiry: 3rd Friday of contract month (CFFEX)."""
        import calendar as cal_mod

        # Third Friday
        c = cal_mod.monthcalendar(self.year, self.month)
        # Fridays are weekday 4; find third occurrence
        fridays = [week[4] for week in c if week[4] != 0]
        day = fridays[2] if len(fridays) >= 3 else fridays[-1]
        return date(self.year, self.month, day)

    def __repr__(self) -> str:
        return f"ContractMonth({self.code})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ContractMonth):
            return False
        return self.code == other.code

    def __hash__(self) -> int:
        return hash(self.code)


class FuturesRolloverDetector:
    """
    Detects dominant contract switches for Chinese futures.

    Detect an intended roll and audit its cost; actual orders require approval.

    Rollover trigger: volume of next contract > current dominant
    for 3 consecutive days (configurable).
    """

    def __init__(
        self,
        underlying: str,
        policy: FuturesRolloverPolicy,
        audit_bus: AuditBus | None = None,
        *,
        _configuration_token: object | None = None,
    ) -> None:
        if _configuration_token is not _ROLLOVER_CONFIGURATION_TOKEN:
            raise ValueError("rollover detector must be created from configured MarketRouter")
        self.underlying = underlying
        self.policy = policy
        self.consecutive_days = self.policy.consecutive_volume_days
        self._audit = audit_bus or AuditBus()
        self._dominant: ContractMonth | None = None
        self._volume_history: dict[ContractMonth, int] = {}
        self._leading_contract: ContractMonth | None = None
        self._next_contract_lead_days: int = 0
        self._last_observation_date: date | None = None

    @property
    def dominant_contract(self) -> ContractMonth | None:
        return self._dominant

    def update_volume(self, contract: ContractMonth, volume: float) -> None:
        """Update volume data and check for rollover signal."""
        self._validate_contract_volume(contract, volume)
        self._volume_history[contract] = self._volume_history.get(contract, 0) + int(volume)

        # Determine current dominant (highest volume last 5 days)
        # Simplified: just track latest dominant
        if self._dominant is None:
            self._dominant = contract
            return

    def _validate_contract_volume(self, contract: ContractMonth, volume: float) -> None:
        """Reject malformed observations before they influence rollover intent."""
        if contract.underlying != self.underlying:
            raise ValueError(
                f"contract {contract.code} does not belong to underlying {self.underlying}"
            )
        if not math.isfinite(volume) or volume < 0:
            raise ValueError("contract volume must be finite and non-negative")

    def _reject_observation(self, today: date, reason: str) -> None:
        """Audit rejected daily observations without changing rollover state."""
        self._audit.record(
            "futures_rollover_observation_rejected",
            "futures_rollover_detector",
            {"underlying": self.underlying, "date": today.isoformat(), "reason": reason},
        )

    def check_rollover(
        self, contracts: list[tuple[ContractMonth, float]], today: date
    ) -> tuple[ContractMonth, ContractMonth] | None:
        """
        Check if dominant should switch.

        Args:
            contracts: List of (contract, today_volume) tuples.
            today: Current date.

        Returns:
            (old_dominant, new_dominant) if rollover detected, else None.
        """
        if not contracts or self._dominant is None:
            return None

        if self._last_observation_date is not None and today <= self._last_observation_date:
            reason = "rollover observations must use strictly increasing trading dates"
            self._reject_observation(today, reason)
            raise ValueError(reason)

        contract_codes = [contract.code for contract, _volume in contracts]
        if len(contract_codes) != len(set(contract_codes)):
            reason = "rollover observation contains duplicate contracts"
            self._reject_observation(today, reason)
            raise ValueError(reason)
        if self._dominant.code not in contract_codes:
            reason = "rollover observation must include the current dominant contract"
            self._reject_observation(today, reason)
            raise ValueError(reason)
        try:
            for contract, volume in contracts:
                self._validate_contract_volume(contract, volume)
        except ValueError as exc:
            self._reject_observation(today, str(exc))
            raise

        max_vol = 0.0
        max_contract = self._dominant
        for ct, vol in contracts:
            if vol > max_vol:
                max_vol = vol
                max_contract = ct
        if (
            max_contract != self._dominant
            and max_contract.expiry_date <= self._dominant.expiry_date
        ):
            reason = "rollover target must expire after the current dominant contract"
            self._reject_observation(today, reason)
            raise ValueError(reason)

        # Apply only a fully validated daily observation.
        for ct, vol in contracts:
            self.update_volume(ct, vol)
        self._last_observation_date = today

        # Check if next contract overtakes.
        if max_contract != self._dominant:
            if max_contract == self._leading_contract:
                self._next_contract_lead_days += 1
            else:
                self._leading_contract = max_contract
                self._next_contract_lead_days = 1
        else:
            self._leading_contract = None
            self._next_contract_lead_days = 0

        if self._next_contract_lead_days >= self.consecutive_days:
            old = self._dominant
            new = max_contract
            self._dominant = new
            self._leading_contract = None
            self._next_contract_lead_days = 0
            self._audit.record(
                "futures_rollover_intent_detected",
                "futures_rollover_detector",
                {
                    "underlying": self.underlying,
                    "old_contract": old.code,
                    "new_contract": new.code,
                    "date": today.isoformat(),
                    "requires_manual_approval": self.policy.require_manual_approval,
                    "execution_authorized": False,
                },
            )
            return (old, new)

        return None

    def rollover_cost_estimate(
        self, old: ContractMonth, new: ContractMonth, position: float, current_price: float
    ) -> dict[str, float]:
        """
        Estimate the cost of rolling over a futures position.

        Returns:
            dict with estimated spread cost, commission, and total.
        """
        if current_price <= 0:
            raise ValueError("current_price must be positive")
        notional = abs(position) * current_price
        spread_cost = notional * self.policy.spread_cost_bps / 10_000
        commission = notional * self.policy.commission_bps_per_leg / 10_000 * 2
        estimate = {
            "spread_cost": round(spread_cost, 2),
            "commission": round(commission, 2),
            "total": round(spread_cost + commission, 2),
        }
        self._audit.record(
            "futures_rollover_cost_estimated",
            "futures_rollover_detector",
            {
                "underlying": self.underlying,
                "old_contract": old.code,
                "new_contract": new.code,
                "notional": notional,
                "spread_cost_bps": self.policy.spread_cost_bps,
                "commission_bps_per_leg": self.policy.commission_bps_per_leg,
                **estimate,
            },
        )
        return estimate


# ── Market adapter router ─────────────────────────────────────────────────────


class MarketRouter:
    """
    Routes market-specific behavior: data sources, risk rules, fee models.

    AGENTS.md §2 (config-002):
      "数据源根据 primary_markets 自动选择合适的数据接口"
      "风控模块根据市场类型启用不同规则"
      "税费计算根据市场自动切换费率模型"
    """

    _rollover_policy: ClassVar[FuturesRolloverPolicy | None] = None

    @classmethod
    def configure(cls, settings: QuantSettings) -> None:
        """Reject direct publication; runtime settings must pass ConfigLoader gates."""
        del settings
        raise PermissionError("market routing configuration must be published by ConfigLoader")

    @classmethod
    def _publish_loaded_snapshot(
        cls, settings: QuantSettings, *, _publication_token: object | None = None
    ) -> None:
        """Publish a validated loader snapshot at the internal trust boundary."""
        prepared = cls._prepare_loaded_snapshot(settings, _publication_token=_publication_token)
        cls._publish_prepared_snapshot(settings, prepared, _publication_token=_publication_token)

    @classmethod
    def _validate_loaded_snapshot(
        cls, settings: QuantSettings, *, _publication_token: object | None = None
    ) -> None:
        """Validate a loader candidate before an audit-authorized publication."""
        cls._prepare_loaded_snapshot(settings, _publication_token=_publication_token)

    @classmethod
    def _prepare_loaded_snapshot(
        cls, settings: QuantSettings, *, _publication_token: object | None = None
    ) -> tuple[dict[Market, MarketCalendar], dict[Market, MarketRules], set[Market]]:
        """Resolve all external validation before an audited in-memory commit."""
        if _publication_token is not _RUNTIME_PUBLICATION_TOKEN:
            raise PermissionError("market routing configuration must be published by ConfigLoader")
        if not settings.config_hash.strip():
            raise ValueError("market routing requires a hash-bound configuration snapshot")
        if settings.system.env == Environment.PROD and not settings.config_approval_ref.strip():
            raise ValueError("production market routing requires configuration approval")
        return CalendarRegistry._prepare_validated(settings.system)

    @classmethod
    def _publish_prepared_snapshot(
        cls,
        settings: QuantSettings,
        prepared: tuple[dict[Market, MarketCalendar], dict[Market, MarketRules], set[Market]],
        *,
        _publication_token: object | None = None,
    ) -> None:
        """Commit a prevalidated candidate after the loader records authorization."""
        if _publication_token is not _RUNTIME_PUBLICATION_TOKEN:
            raise PermissionError("market routing configuration must be published by ConfigLoader")
        CalendarRegistry._publish_prepared(prepared)
        cls._rollover_policy = settings.system.futures_rollover

    @classmethod
    def _configure_for_testing(cls, settings: SystemSettings) -> None:
        """Install already validated system models for isolated tests only."""
        cls._install_system_settings(settings)

    @classmethod
    def _install_system_settings(cls, settings: SystemSettings) -> None:
        CalendarRegistry._configure_validated(settings)
        cls._rollover_policy = settings.futures_rollover

    @classmethod
    def create_futures_rollover_detector(
        cls, underlying: str, audit_bus: AuditBus | None = None
    ) -> FuturesRolloverDetector:
        """Build rollover detection only from hash-bound validated settings."""
        if not CalendarRegistry.is_active(Market.FUTURES):
            raise ValueError("futures market must be enabled before rollover detection")
        if cls._rollover_policy is None:
            raise ValueError("market router must be configured before rollover detection")
        return FuturesRolloverDetector(
            underlying,
            policy=cls._rollover_policy,
            audit_bus=audit_bus,
            _configuration_token=_ROLLOVER_CONFIGURATION_TOKEN,
        )

    @classmethod
    def get_data_sources(cls, market: Market) -> list[str]:
        """Get approved data source order for an enabled market."""
        if not CalendarRegistry.is_active(market):
            raise ValueError(f"{market.value} is not enabled in primary_markets")
        return list(CalendarRegistry.get_rules(market).data_sources)

    @classmethod
    def get_fee_model(cls, market: Market) -> str:
        """Get approved fee/tax model identifier for an enabled market."""
        if not CalendarRegistry.is_active(market):
            raise ValueError(f"{market.value} is not enabled in primary_markets")
        return CalendarRegistry.get_rules(market).fee_model

    @classmethod
    def get_rules(cls, market: Market) -> MarketRuleSet:
        """Get configured trading rules for a market; never infer production rules."""
        return MarketRuleSet.from_config(CalendarRegistry.get_rules(market))

    @classmethod
    def get_calendar(cls, market: Market) -> MarketCalendar:
        """Get trading calendar for a market."""
        return CalendarRegistry.get(market)

    @classmethod
    def is_trading_allowed(
        cls,
        market: Market,
        dt: datetime | None = None,
        *,
        allow_settlement_window: bool = False,
    ) -> tuple[bool, str]:
        """
        Check if trading is allowed for a market at a given time.

        Returns:
            (allowed, reason) tuple.
        """
        if not CalendarRegistry.is_active(market):
            return False, f"{market.value} is not enabled in primary_markets"
        try:
            cal = cls.get_calendar(market)
        except ValueError as exc:
            return False, str(exc)
        check_dt = dt or datetime.now(UTC)

        try:
            if not cal.is_in_session(check_dt):
                return False, f"{market.value} is not in trading session"

            from .market_calendar import SettlementWindow

            if not allow_settlement_window and SettlementWindow.is_in_settlement_window(
                market, check_dt
            ):
                return False, f"{market.value} is in settlement window (new positions restricted)"
        except ValueError as exc:
            return False, str(exc)

        return True, "ok"
