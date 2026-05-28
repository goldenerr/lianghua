"""
Configuration-driven market calendar and settlement-window management.

All decision inputs must be timezone-aware; they are normalized to UTC before
being evaluated against market-local schedules from ``config/system.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import ClassVar
from zoneinfo import ZoneInfo

import pandas as pd

from .settings import Market, MarketRules, SystemSettings, TradingSession

UTC = timezone.utc


class CalendarUnavailableError(ValueError):
    """A required exchange holiday calendar cannot be trusted for decisions."""


def _require_utc(dt: datetime) -> datetime:
    """Normalize an aware timestamp to UTC and fail closed for naive values."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("market calendar timestamps must be timezone-aware")
    return dt.astimezone(UTC)


def _clock_minutes(value: str) -> int:
    if value == "24:00":
        return 24 * 60
    parsed = time.fromisoformat(value)
    return parsed.hour * 60 + parsed.minute


@dataclass(frozen=True)
class Session:
    """One market-local trading interval, supporting overnight sessions."""

    opening_minute: int
    closing_minute: int
    label: str = ""

    @classmethod
    def from_config(cls, hours: tuple[str, str], label: str = "") -> Session:
        return cls(_clock_minutes(hours[0]), _clock_minutes(hours[1]), label)

    @property
    def is_overnight(self) -> bool:
        return self.closing_minute < self.opening_minute

    @property
    def is_full_day(self) -> bool:
        return self.opening_minute == 0 and self.closing_minute == 24 * 60

    def contains(self, local_dt: datetime) -> bool:
        minute = local_dt.hour * 60 + local_dt.minute
        if self.is_full_day:
            return True
        if self.is_overnight:
            return minute >= self.opening_minute or minute < self.closing_minute
        return self.opening_minute <= minute < self.closing_minute

    def trading_date(self, local_dt: datetime) -> date:
        minute = local_dt.hour * 60 + local_dt.minute
        if self.is_overnight and minute < self.closing_minute:
            return local_dt.date() - timedelta(days=1)
        return local_dt.date()

    def opening_at(self, trading_date: date, zone: ZoneInfo) -> datetime:
        hour, minute = divmod(self.opening_minute, 60)
        return datetime.combine(trading_date, time(hour, minute), tzinfo=zone)

    def closing_at(self, trading_date: date, zone: ZoneInfo) -> datetime:
        close_date = trading_date + timedelta(days=1) if self.is_overnight else trading_date
        minute_value = 0 if self.closing_minute == 24 * 60 else self.closing_minute
        if self.closing_minute == 24 * 60:
            close_date += timedelta(days=1)
        hour, minute = divmod(minute_value, 60)
        return datetime.combine(close_date, time(hour, minute), tzinfo=zone)


class MarketCalendar:
    """A configured calendar evaluated with UTC decision timestamps."""

    def __init__(self, configured: TradingSession):
        self.market = configured.market
        self.timezone = ZoneInfo(configured.timezone)
        self.holiday_calendar = configured.holiday_calendar
        self.sessions = [
            Session.from_config(hours, f"{configured.market.value}-{index}")
            for index, hours in enumerate(configured.sessions)
        ]
        self._holidays_by_year: dict[int, set[date]] = {}

    @property
    def is_24_7(self) -> bool:
        return len(self.sessions) == 1 and self.sessions[0].is_full_day

    def to_market_time(self, dt: datetime) -> datetime:
        """Convert an aware decision timestamp to the configured local display time."""
        return _require_utc(dt).astimezone(self.timezone)

    def validate_holiday_calendar(self) -> None:
        """Fail startup when an active exchange calendar cannot be instantiated."""
        if self.is_24_7:
            return
        cal_code = self.holiday_calendar
        if cal_code is None:
            raise CalendarUnavailableError(
                f"{self.market.value} has no approved holiday calendar configured"
            )
        try:
            import pandas_market_calendars as mcal

            mcal.get_calendar(cal_code)
        except Exception as exc:
            raise CalendarUnavailableError(
                f"{self.market.value} holiday calendar cannot be initialized"
            ) from exc

    def is_trading_day(self, trading_date: date) -> bool:
        if self.is_24_7:
            return True
        if trading_date.weekday() >= 5:
            return False
        return trading_date not in self._get_holidays(trading_date.year)

    def is_in_session(self, dt: datetime) -> bool:
        local_dt = self.to_market_time(dt)
        if self.is_24_7:
            return True
        for session in self.sessions:
            if session.contains(local_dt) and self.is_trading_day(session.trading_date(local_dt)):
                return True
        return False

    def next_trading_day(self, candidate: date) -> date:
        while not self.is_trading_day(candidate):
            candidate += timedelta(days=1)
        return candidate

    def trading_days_between(self, start: date, end: date) -> list[date]:
        days: list[date] = []
        candidate = start
        while candidate <= end:
            if self.is_trading_day(candidate):
                days.append(candidate)
            candidate += timedelta(days=1)
        return days

    def time_to_next_open(self, dt: datetime) -> timedelta | None:
        if self.is_24_7:
            return None
        utc_dt = _require_utc(dt)
        local_dt = utc_dt.astimezone(self.timezone)
        for offset in range(0, 14):
            trading_date = local_dt.date() + timedelta(days=offset)
            if not self.is_trading_day(trading_date):
                continue
            for session in self.sessions:
                opening = session.opening_at(trading_date, self.timezone).astimezone(UTC)
                if opening > utc_dt:
                    return opening - utc_dt
        return None

    def time_to_close(self, dt: datetime) -> timedelta | None:
        if self.is_24_7:
            return None
        utc_dt = _require_utc(dt)
        local_dt = utc_dt.astimezone(self.timezone)
        for session in self.sessions:
            trading_date = session.trading_date(local_dt)
            if session.contains(local_dt) and self.is_trading_day(trading_date):
                closing = session.closing_at(trading_date, self.timezone).astimezone(UTC)
                return closing - utc_dt
        return None

    def _get_holidays(self, year: int) -> set[date]:
        if year in self._holidays_by_year:
            return self._holidays_by_year[year]
        cal_code = self.holiday_calendar
        if cal_code is None:
            raise CalendarUnavailableError(
                f"{self.market.value} has no approved holiday calendar configured"
            )
        try:
            import pandas_market_calendars as mcal

            schedule = mcal.get_calendar(cal_code).schedule(
                start_date=f"{year}-01-01",
                end_date=f"{year}-12-31",
            )
            if schedule.empty:
                raise CalendarUnavailableError(
                    f"{self.market.value} holiday calendar returned no sessions for {year}"
                )
            all_dates = pd.bdate_range(f"{year}-01-01", f"{year}-12-31")
            trading_dates = set(schedule.index.date)
            holidays = {day.date() for day in all_dates if day.date() not in trading_dates}
        except CalendarUnavailableError:
            raise
        except Exception as exc:
            raise CalendarUnavailableError(
                f"{self.market.value} holiday calendar unavailable for {year}"
            ) from exc
        self._holidays_by_year[year] = holidays
        return holidays


class CalendarRegistry:
    """Runtime registry initialized only from validated system configuration."""

    _calendars: ClassVar[dict[Market, MarketCalendar]] = {}
    _rules: ClassVar[dict[Market, MarketRules]] = {}
    _active_markets: ClassVar[set[Market]] = set()

    @classmethod
    def _prepare_validated(
        cls, settings: SystemSettings
    ) -> tuple[dict[Market, MarketCalendar], dict[Market, MarketRules], set[Market]]:
        """Build and validate candidate controls without changing runtime state."""
        calendars = {
            configured.market: MarketCalendar(configured)
            for configured in settings.trading_sessions
        }
        rules = {configured.market: configured for configured in settings.market_rules}
        active_markets = set(settings.primary_markets)
        for market in active_markets:
            calendars[market].validate_holiday_calendar()
        return calendars, rules, active_markets

    @classmethod
    def _configure_validated(cls, settings: SystemSettings) -> None:
        """Publish market controls only after the upper configuration gate approves them."""
        cls._publish_prepared(cls._prepare_validated(settings))

    @classmethod
    def _publish_prepared(
        cls,
        prepared: tuple[dict[Market, MarketCalendar], dict[Market, MarketRules], set[Market]],
    ) -> None:
        """Install previously validated controls without a second external lookup."""
        calendars, rules, active_markets = prepared
        # Publish only a fully validated registry; a failed reload keeps prior controls live.
        cls._calendars = calendars
        cls._rules = rules
        cls._active_markets = active_markets

    @classmethod
    def _configure_for_testing(cls, settings: SystemSettings) -> None:
        """Install validated model instances for isolated unit tests only."""
        cls._configure_validated(settings)

    @classmethod
    def is_active(cls, market: Market) -> bool:
        return market in cls._active_markets

    @classmethod
    def get(cls, market: Market) -> MarketCalendar:
        if not cls.is_active(market):
            raise ValueError(f"{market.value} is not enabled in primary_markets")
        try:
            return cls._calendars[market]
        except KeyError as exc:
            raise ValueError(f"market has no configured trading session: {market.value}") from exc

    @classmethod
    def get_rules(cls, market: Market) -> MarketRules:
        if not cls.is_active(market):
            raise ValueError(f"{market.value} is not enabled in primary_markets")
        try:
            return cls._rules[market]
        except KeyError as exc:
            raise ValueError(f"market has no configured rules: {market.value}") from exc

    @classmethod
    def is_any_market_open(cls, markets: list[Market], dt: datetime | None = None) -> bool:
        check_dt = dt or datetime.now(UTC)
        for market in markets:
            try:
                if cls.get(market).is_in_session(check_dt):
                    return True
            except ValueError:
                # Aggregated availability queries must never treat uncertainty as open.
                continue
        return False

    @classmethod
    def all_markets_closed(cls, markets: list[Market], dt: datetime | None = None) -> bool:
        return not cls.is_any_market_open(markets, dt)


class SettlementWindow:
    """Configured local settlement restriction, normalized from UTC timestamps."""

    @classmethod
    def is_in_settlement_window(cls, market: Market, dt: datetime | None = None) -> bool:
        rules = CalendarRegistry.get_rules(market)
        if rules.settlement_time is None or rules.settlement_window_minutes == 0:
            return False
        calendar = CalendarRegistry.get(market)
        utc_dt = _require_utc(dt or datetime.now(UTC))
        local_dt = utc_dt.astimezone(calendar.timezone)
        settle_minute = _clock_minutes(rules.settlement_time)
        hour, minute = divmod(settle_minute, 60)
        settlement_local = datetime.combine(local_dt.date(), time(hour, minute), calendar.timezone)
        window_start = settlement_local - timedelta(minutes=rules.settlement_window_minutes)
        return window_start.astimezone(UTC) <= utc_dt < settlement_local.astimezone(UTC)
