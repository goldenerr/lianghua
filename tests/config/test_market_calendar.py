"""Tests for market calendar, session detection, and settlement windows."""

from datetime import date, datetime, timezone

from quant_trading.config.market_calendar import (
    CalendarRegistry,
    Market,
    MarketCalendar,
    Session,
    SettlementWindow,
)

UTC = timezone.utc


# ── MarketCalendar ────────────────────────────────────────────────────────────


class TestMarketCalendar:
    """Test trading day and session detection."""

    def test_crypto_always_in_session(self):
        cal = MarketCalendar(Market.CRYPTO)
        # Saturday midnight UTC
        dt = datetime(2026, 5, 16, 2, 0, tzinfo=UTC)  # Saturday
        assert cal.is_in_session(dt) is True
        assert cal.is_trading_day(dt.date()) is True
        assert cal.is_24_7 is True

    def test_a_share_weekday_morning(self):
        cal = MarketCalendar(Market.A_SHARES)
        # Monday 10:00 CST = 2:00 UTC
        dt = datetime(2026, 5, 18, 2, 0, tzinfo=UTC)  # Monday
        # is_trading_day might fail if pandas_market_calendars is not installed
        # but fallback should work for non-holiday weekdays
        assert cal.is_trading_day(dt.date()) is True
        assert cal.is_in_session(dt) is True  # In morning session

    def test_a_share_weekday_lunch_break(self):
        cal = MarketCalendar(Market.A_SHARES)
        # Monday 12:00 CST = 4:00 UTC (lunch break)
        dt = datetime(2026, 5, 18, 4, 0, tzinfo=UTC)
        assert cal.is_trading_day(dt.date()) is True
        assert cal.is_in_session(dt) is False  # Lunch break

    def test_a_share_weekend(self):
        cal = MarketCalendar(Market.A_SHARES)
        dt = datetime(2026, 5, 16, 2, 0, tzinfo=UTC)  # Saturday
        assert cal.is_trading_day(dt.date()) is False
        assert cal.is_in_session(dt) is False

    def test_futures_night_session(self):
        cal = MarketCalendar(Market.FUTURES)
        # Monday night 22:00 CST = 14:00 UTC
        dt = datetime(2026, 5, 18, 14, 0, tzinfo=UTC)
        assert cal.is_trading_day(dt.date()) is True
        assert cal.is_in_session(dt) is True  # Night session active

    def test_futures_between_sessions(self):
        cal = MarketCalendar(Market.FUTURES)
        # Between afternoon close (7:00 UTC) and night open (13:00 UTC)
        dt = datetime(2026, 5, 18, 10, 0, tzinfo=UTC)
        assert cal.is_in_session(dt) is False

    def test_us_stock_regular_hours(self):
        cal = MarketCalendar(Market.US_STOCKS)
        # 10:00 EST = 15:00 UTC
        dt = datetime(2026, 5, 18, 15, 0, tzinfo=UTC)
        assert cal.is_trading_day(dt.date()) is True
        assert cal.is_in_session(dt) is True

    def test_next_trading_day(self):
        cal = MarketCalendar(Market.A_SHARES)
        # Saturday → should return Monday
        sat = date(2026, 5, 16)
        next_td = cal.next_trading_day(sat)
        assert next_td.weekday() < 5  # Must be weekday
        assert next_td > sat

    def test_time_to_next_open(self):
        cal = MarketCalendar(Market.A_SHARES)
        # Lunch break 12:00 CST = 4:00 UTC
        dt = datetime(2026, 5, 18, 4, 0, tzinfo=UTC)
        delta = cal.time_to_next_open(dt)
        assert delta is not None
        # Next session (afternoon 13:00 CST = 5:00 UTC) — 1 hour away
        assert delta.total_seconds() == 3600

    def test_time_to_close(self):
        cal = MarketCalendar(Market.A_SHARES)
        # Morning session, 30 min before close
        dt = datetime(2026, 5, 18, 3, 0, tzinfo=UTC)  # 11:00 CST
        delta = cal.time_to_close(dt)
        assert delta is not None
        assert delta.total_seconds() == 1800  # 30 min to 11:30 CST = 3:30 UTC

    def test_naive_datetime_treated_as_utc(self):
        cal = MarketCalendar(Market.A_SHARES)
        dt = datetime(2026, 5, 18, 2, 0)  # Naive
        assert cal.is_in_session(dt) is True


# ── CalendarRegistry ──────────────────────────────────────────────────────────


class TestCalendarRegistry:
    def test_singleton_per_market(self):
        a1 = CalendarRegistry.get(Market.A_SHARES)
        a2 = CalendarRegistry.get(Market.A_SHARES)
        assert a1 is a2

    def test_different_markets(self):
        a = CalendarRegistry.get(Market.A_SHARES)
        c = CalendarRegistry.get(Market.CRYPTO)
        assert a is not c

    def test_is_any_market_open(self):
        # Crypto is always open
        assert CalendarRegistry.is_any_market_open([Market.CRYPTO]) is True

    def test_all_markets_closed_weekend(self):
        # Saturday — A-shares and futures closed, crypto open
        dt = datetime(2026, 5, 16, 2, 0, tzinfo=UTC)
        assert CalendarRegistry.is_any_market_open([Market.A_SHARES], dt) is False
        assert CalendarRegistry.all_markets_closed([Market.A_SHARES], dt) is True


# ── SettlementWindow ──────────────────────────────────────────────────────────


class TestSettlementWindow:
    def test_a_share_settlement_window(self):
        """16:00 CST settlement → 08:00 UTC. Window starts 07:30 UTC."""
        # 07:45 UTC = inside window
        dt = datetime(2026, 5, 18, 7, 45, tzinfo=UTC)
        assert SettlementWindow.is_in_settlement_window(Market.A_SHARES, dt) is True

    def test_a_share_outside_window(self):
        """Before settlement window."""
        dt = datetime(2026, 5, 18, 7, 0, tzinfo=UTC)
        assert SettlementWindow.is_in_settlement_window(Market.A_SHARES, dt) is False

    def test_crypto_no_settlement(self):
        dt = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
        assert SettlementWindow.is_in_settlement_window(Market.CRYPTO, dt) is False

    def test_futures_settlement(self):
        """15:30 CST → 07:30 UTC. Window starts 07:00."""
        dt = datetime(2026, 5, 18, 7, 15, tzinfo=UTC)
        assert SettlementWindow.is_in_settlement_window(Market.FUTURES, dt) is True


# ── Session ───────────────────────────────────────────────────────────────────


class TestSession:
    def test_contains(self):
        from datetime import time as dt_time

        s = Session(dt_time(1, 30), dt_time(3, 30), "morning")
        dt = datetime(2026, 5, 18, 2, 0, tzinfo=UTC)
        assert s.contains(dt) is True

    def test_not_contains_before(self):
        from datetime import time as dt_time

        s = Session(dt_time(1, 30), dt_time(3, 30), "morning")
        dt = datetime(2026, 5, 18, 1, 0, tzinfo=UTC)
        assert s.contains(dt) is False

    def test_not_contains_after(self):
        from datetime import time as dt_time

        s = Session(dt_time(1, 30), dt_time(3, 30), "morning")
        dt = datetime(2026, 5, 18, 4, 0, tzinfo=UTC)
        assert s.contains(dt) is False
