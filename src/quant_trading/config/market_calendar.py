"""
Market calendar & trading session management.

Integrates pandas_market_calendars for holiday detection.
Unified UTC internal time representation.
Supports: A-shares, futures (with night session), crypto (24/7), US/HK stocks.

AGENTS.md §30: 多时区与交易时段管理
AGENTS.md §2 (config-002): trading_sessions, primary_markets, market_rules
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from enum import Enum

import pandas as pd

UTC = timezone.utc

# ── Market identifiers ────────────────────────────────────────────────────────


class Market(str, Enum):
    """Trading market identifier. Must match config/system.yaml primary_markets."""
    A_SHARES = "A股"
    FUTURES = "期货"
    CRYPTO = "加密货币"
    US_STOCKS = "美股"
    HK_STOCKS = "港股"
    OPTIONS = "期权"


# pandas_market_calendars exchange codes
_MARKET_TO_CALENDAR: dict[Market, str] = {
    Market.A_SHARES: "XSHG",       # Shanghai Stock Exchange
    Market.US_STOCKS: "NYSE",
    Market.HK_STOCKS: "HKEX",
    # Futures and Crypto — custom calendar (24/7 minus known holidays)
    Market.FUTURES: "XSHG",        # Uses Shanghai calendar + night session logic
}


# ── Trading hours config ──────────────────────────────────────────────────────


class Session:
    """A single continuous trading session (e.g., morning, afternoon, night)."""

    def __init__(self, open_utc: time, close_utc: time, label: str = ""):
        self.open = open_utc
        self.close = close_utc
        self.label = label

    def contains(self, dt: datetime) -> bool:
        """Check if a UTC datetime falls within this session."""
        t = dt.time()
        return self.open <= t < self.close

    def __repr__(self) -> str:
        return f"Session({self.label} {self.open}-{self.close})"


# Pre-configured market sessions (all in UTC)
# AGENTS.md §30: 加密货币 24/7, A股 9:30-11:30,13:00-15:00, 期货含夜盘

A_SHARE_SESSIONS = [
    Session(time(1, 30), time(3, 30), "上午"),   # 9:30-11:30 CST = 1:30-3:30 UTC
    Session(time(5, 0), time(7, 0), "下午"),     # 13:00-15:00 CST = 5:00-7:00 UTC
]

FUTURES_SESSIONS = [
    Session(time(1, 30), time(3, 30), "日盘上午"),
    Session(time(5, 0), time(7, 0), "日盘下午"),
    Session(time(13, 0), time(18, 30), "夜盘"),  # 21:00-02:30 CST = 13:00-18:30 UTC
]

US_STOCK_SESSIONS = [
    Session(time(14, 30), time(21, 0), "regular"),  # 9:30-16:00 EST = 14:30-21:00 UTC
]

CRYPTO_SESSIONS = [
    Session(time(0, 0), time(23, 59, 59, 999999), "24/7"),
]

HK_STOCK_SESSIONS = [
    Session(time(1, 30), time(4, 0), "上午"),    # 9:30-12:00 HKT = 1:30-4:00 UTC
    Session(time(5, 0), time(8, 0), "下午"),     # 13:00-16:00 HKT = 5:00-8:00 UTC
]

_MARKET_SESSIONS: dict[Market, list[Session]] = {
    Market.A_SHARES: A_SHARE_SESSIONS,
    Market.FUTURES: FUTURES_SESSIONS,
    Market.CRYPTO: CRYPTO_SESSIONS,
    Market.US_STOCKS: US_STOCK_SESSIONS,
    Market.HK_STOCKS: HK_STOCK_SESSIONS,
}


# ── Calendar utils ────────────────────────────────────────────────────────────


class MarketCalendar:
    """
    Trading calendar for a specific market.

    Caches holiday lookups. Falls back to weekend-only calendar
    if pandas_market_calendars is unavailable.
    """

    def __init__(self, market: Market):
        self.market = market
        self.sessions = _MARKET_SESSIONS.get(market, [])
        self._calendar: object | None = None
        self._holidays: set[date] | None = None

    @property
    def is_24_7(self) -> bool:
        return self.market == Market.CRYPTO

    # ── Trading day checks ────────────────────────────────────────────────

    def is_trading_day(self, d: date) -> bool:
        """Check if a date is a trading day (not weekend, not holiday)."""
        if self.is_24_7:
            return True
        if d.weekday() >= 5:  # Saturday=5, Sunday=6
            return False
        return d not in self._get_holidays()

    def is_in_session(self, dt: datetime) -> bool:
        """
        Check if a UTC datetime falls within any trading session
        on a trading day.
        """
        if self.is_24_7:
            return True
        utc_dt = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        d = utc_dt.date()

        if not self.is_trading_day(d):
            return False

        return any(s.contains(utc_dt) for s in self.sessions)

    def next_trading_day(self, d: date) -> date:
        """Return the next trading day on or after the given date."""
        if self.is_24_7:
            return d
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d

    def trading_days_between(self, start: date, end: date) -> list[date]:
        """Return all trading days in [start, end]."""
        if self.is_24_7:
            return pd.bdate_range(start, end).tolist()  # type: ignore[return-value]
        days: list[date] = []
        current = start
        while current <= end:
            if self.is_trading_day(current):
                days.append(current)
            current += timedelta(days=1)
        return days

    def time_to_next_open(self, dt: datetime) -> timedelta | None:
        """Time until the next trading session opens. None if 24/7."""
        if self.is_24_7 or not self.sessions:
            return None

        utc_dt = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        today = utc_dt.date()

        # Check remaining sessions today
        for session in self.sessions:
            session_start = datetime.combine(today, session.open, tzinfo=UTC)
            if utc_dt < session_start:
                return session_start - utc_dt

        # Next trading day's first session
        next_day = today + timedelta(days=1)
        while not self.is_trading_day(next_day):
            next_day += timedelta(days=1)

        first_session = self.sessions[0]
        next_open = datetime.combine(next_day, first_session.open, tzinfo=UTC)
        return next_open - utc_dt

    def time_to_close(self, dt: datetime) -> timedelta | None:
        """Time until the current session closes. None if 24/7 or out of session."""
        if self.is_24_7:
            return None

        utc_dt = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        today = utc_dt.date()

        if not self.is_trading_day(today):
            return None

        for session in self.sessions:
            if session.contains(utc_dt):
                close = datetime.combine(today, session.close, tzinfo=UTC)
                return close - utc_dt

        return None

    # ── Internal ──────────────────────────────────────────────────────────

    def _get_holidays(self) -> set[date]:
        """Lazy-load holidays from pandas_market_calendars."""
        if self._holidays is not None:
            return self._holidays

        cal_code = _MARKET_TO_CALENDAR.get(self.market)
        if cal_code is None:
            self._holidays = set()
            return self._holidays

        try:
            import pandas_market_calendars as mcal

            cal = mcal.get_calendar(cal_code)
            # Get holidays for current year ± 1
            today = date.today()
            schedule = cal.schedule(
                start_date=str(today.year - 1),
                end_date=str(today.year + 1),
            )
            # business days that are NOT in schedule = close/early close days
            all_dates = pd.bdate_range(schedule.index[0], schedule.index[-1])
            trading_dates = set(schedule.index.date)
            holidays = {d.date() for d in all_dates if d.date() not in trading_dates}
            self._holidays = holidays
        except ImportError:
            # Fallback: no holiday calendar, just weekends
            self._holidays = set()
        except Exception:
            self._holidays = set()

        return self._holidays


# ── Global registry ───────────────────────────────────────────────────────────


class CalendarRegistry:
    """Global registry of market calendars, keyed by Market enum."""

    _calendars: dict[Market, MarketCalendar] = {}

    @classmethod
    def get(cls, market: Market) -> MarketCalendar:
        """Get or create a MarketCalendar for the given market."""
        if market not in cls._calendars:
            cls._calendars[market] = MarketCalendar(market)
        return cls._calendars[market]

    @classmethod
    def is_any_market_open(cls, markets: list[Market], dt: datetime | None = None) -> bool:
        """Check if any of the given markets is in session."""
        check_dt = dt or datetime.now(UTC)
        return any(cls.get(m).is_in_session(check_dt) for m in markets)

    @classmethod
    def all_markets_closed(cls, markets: list[Market], dt: datetime | None = None) -> bool:
        """Check if all given markets are closed."""
        return not cls.is_any_market_open(markets, dt)


# ── Settlement window detector ────────────────────────────────────────────────


class SettlementWindow:
    """
    Detects settlement windows for each market.
    AGENTS.md §57: 结算前30分钟限制开仓，仅允许平仓。
    """

    # Settlement times in UTC
    SETTLEMENT_TIMES: dict[Market, time] = {
        Market.A_SHARES: time(8, 0),     # T+1 16:00 CST → 08:00 UTC
        Market.FUTURES: time(7, 30),     # 15:30 CST → 07:30 UTC
        Market.HK_STOCKS: time(8, 0),    # 16:00 HKT → 08:00 UTC
    }

    WINDOW_MINUTES: int = 30  # Restrict new positions 30 min before settlement

    @classmethod
    def is_in_settlement_window(
        cls, market: Market, dt: datetime | None = None
    ) -> bool:
        """Check if we're in the pre-settlement restricted window."""
        settle_time = cls.SETTLEMENT_TIMES.get(market)
        if settle_time is None:
            return False

        utc_dt = (dt or datetime.now(UTC)).replace(tzinfo=UTC)
        settle_dt = datetime.combine(utc_dt.date(), settle_time, tzinfo=UTC)
        window_start = settle_dt - timedelta(minutes=cls.WINDOW_MINUTES)

        return window_start <= utc_dt < settle_dt
