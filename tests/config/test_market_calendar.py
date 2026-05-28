"""Tests for market calendar, session detection, and settlement windows."""

from datetime import date, datetime, timezone

import pytest
from quant_trading.config.market_calendar import (
    CalendarRegistry,
    CalendarUnavailableError,
    MarketCalendar,
    Session,
    SettlementWindow,
)
from quant_trading.config.settings import Market, MarketRules, SystemSettings, TradingSession

UTC = timezone.utc


@pytest.fixture(autouse=True)
def configured_calendars() -> None:
    CalendarRegistry._configure_for_testing(
        SystemSettings(
            primary_markets=[Market.A_SHARES, Market.FUTURES, Market.CRYPTO, Market.US_STOCKS],
            trading_sessions=[
                TradingSession(
                    market=Market.A_SHARES,
                    sessions=[("09:30", "11:30"), ("13:00", "15:00")],
                    timezone="Asia/Shanghai",
                    holiday_calendar="XSHG",
                ),
                TradingSession(
                    market=Market.FUTURES,
                    sessions=[("09:30", "11:30"), ("13:00", "15:00"), ("21:00", "02:30")],
                    timezone="Asia/Shanghai",
                    holiday_calendar="XSHG",
                ),
                TradingSession(market=Market.CRYPTO, sessions=[("00:00", "24:00")], timezone="UTC"),
                TradingSession(
                    market=Market.US_STOCKS,
                    sessions=[("09:30", "16:00")],
                    timezone="America/New_York",
                    holiday_calendar="NYSE",
                ),
            ],
            market_rules=[
                MarketRules(
                    market=Market.A_SHARES,
                    tick_size=0.01,
                    lot_size=100,
                    price_precision=2,
                    data_sources=["tushare"],
                    fee_model="cn_stock",
                    settlement_time="16:00",
                ),
                MarketRules(
                    market=Market.FUTURES,
                    tick_size=1,
                    lot_size=1,
                    price_precision=0,
                    data_sources=["tushare"],
                    fee_model="cn_futures",
                    settlement_time="15:30",
                ),
                MarketRules(
                    market=Market.CRYPTO,
                    tick_size=0.01,
                    lot_size=1,
                    price_precision=2,
                    data_sources=["ccxt"],
                    fee_model="crypto_maker_taker",
                ),
                MarketRules(
                    market=Market.US_STOCKS,
                    tick_size=0.01,
                    lot_size=1,
                    price_precision=2,
                    data_sources=["polygon"],
                    fee_model="us_stock",
                ),
            ],
        )
    )


# ── MarketCalendar ────────────────────────────────────────────────────────────


class TestMarketCalendar:
    """Test trading day and session detection."""

    def test_crypto_always_in_session(self):
        cal = CalendarRegistry.get(Market.CRYPTO)
        # Saturday midnight UTC
        dt = datetime(2026, 5, 16, 2, 0, tzinfo=UTC)  # Saturday
        assert cal.is_in_session(dt) is True
        assert cal.is_trading_day(dt.date()) is True
        assert cal.is_24_7 is True

    def test_a_share_weekday_morning(self):
        cal = CalendarRegistry.get(Market.A_SHARES)
        # Monday 10:00 CST = 2:00 UTC
        dt = datetime(2026, 5, 18, 2, 0, tzinfo=UTC)  # Monday
        assert cal.is_trading_day(dt.date()) is True
        assert cal.is_in_session(dt) is True  # In morning session

    def test_a_share_exchange_holiday_is_closed(self):
        cal = CalendarRegistry.get(Market.A_SHARES)
        assert cal.is_trading_day(date(2026, 1, 1)) is False

    def test_calendar_provider_failure_fails_closed(self, monkeypatch):
        import pandas_market_calendars as mcal

        def unavailable_calendar(_code: str):
            raise RuntimeError("provider unavailable")

        monkeypatch.setattr(mcal, "get_calendar", unavailable_calendar)
        cal = MarketCalendar(
            TradingSession(
                market=Market.A_SHARES,
                sessions=[("09:30", "11:30")],
                timezone="Asia/Shanghai",
                holiday_calendar="XSHG",
            )
        )
        with pytest.raises(CalendarUnavailableError, match="holiday calendar unavailable"):
            cal.is_trading_day(date(2031, 5, 19))

    def test_market_without_approved_calendar_fails_closed(self):
        cal = MarketCalendar(
            TradingSession(
                market=Market.OPTIONS,
                sessions=[("09:30", "11:30")],
                timezone="Asia/Shanghai",
                holiday_calendar="UNKNOWN_CALENDAR",
            )
        )
        with pytest.raises(CalendarUnavailableError, match="holiday calendar unavailable"):
            cal.is_trading_day(date(2031, 5, 19))

    def test_a_share_weekday_lunch_break(self):
        cal = CalendarRegistry.get(Market.A_SHARES)
        # Monday 12:00 CST = 4:00 UTC (lunch break)
        dt = datetime(2026, 5, 18, 4, 0, tzinfo=UTC)
        assert cal.is_trading_day(dt.date()) is True
        assert cal.is_in_session(dt) is False  # Lunch break

    def test_a_share_weekend(self):
        cal = CalendarRegistry.get(Market.A_SHARES)
        dt = datetime(2026, 5, 16, 2, 0, tzinfo=UTC)  # Saturday
        assert cal.is_trading_day(dt.date()) is False
        assert cal.is_in_session(dt) is False

    def test_futures_night_session(self):
        cal = CalendarRegistry.get(Market.FUTURES)
        # Monday night 22:00 CST = 14:00 UTC
        dt = datetime(2026, 5, 18, 14, 0, tzinfo=UTC)
        assert cal.is_trading_day(dt.date()) is True
        assert cal.is_in_session(dt) is True  # Night session active

    def test_futures_between_sessions(self):
        cal = CalendarRegistry.get(Market.FUTURES)
        # Between afternoon close (7:00 UTC) and night open (13:00 UTC)
        dt = datetime(2026, 5, 18, 10, 0, tzinfo=UTC)
        assert cal.is_in_session(dt) is False

    def test_us_stock_regular_hours(self):
        cal = CalendarRegistry.get(Market.US_STOCKS)
        # 10:00 EST = 15:00 UTC
        dt = datetime(2026, 5, 18, 15, 0, tzinfo=UTC)
        assert cal.is_trading_day(dt.date()) is True
        assert cal.is_in_session(dt) is True

    def test_us_stock_dst_hours_are_derived_from_local_schedule(self):
        cal = CalendarRegistry.get(Market.US_STOCKS)
        summer_open = datetime(2026, 7, 6, 13, 30, tzinfo=UTC)
        winter_open = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
        assert cal.is_in_session(summer_open) is True
        assert cal.is_in_session(winter_open) is True

    def test_futures_overnight_session_uses_prior_trading_date(self):
        cal = CalendarRegistry.get(Market.FUTURES)
        # Tuesday 01:30 CST is the continuation of Monday's night session.
        dt = datetime(2026, 5, 18, 17, 30, tzinfo=UTC)
        assert cal.is_in_session(dt) is True

    def test_next_trading_day(self):
        cal = CalendarRegistry.get(Market.A_SHARES)
        # Saturday → should return Monday
        sat = date(2026, 5, 16)
        next_td = cal.next_trading_day(sat)
        assert next_td.weekday() < 5  # Must be weekday
        assert next_td > sat

    def test_time_to_next_open(self):
        cal = CalendarRegistry.get(Market.A_SHARES)
        # Lunch break 12:00 CST = 4:00 UTC
        dt = datetime(2026, 5, 18, 4, 0, tzinfo=UTC)
        delta = cal.time_to_next_open(dt)
        assert delta is not None
        # Next session (afternoon 13:00 CST = 5:00 UTC) — 1 hour away
        assert delta.total_seconds() == 3600

    def test_time_to_close(self):
        cal = CalendarRegistry.get(Market.A_SHARES)
        # Morning session, 30 min before close
        dt = datetime(2026, 5, 18, 3, 0, tzinfo=UTC)  # 11:00 CST
        delta = cal.time_to_close(dt)
        assert delta is not None
        assert delta.total_seconds() == 1800  # 30 min to 11:30 CST = 3:30 UTC

    def test_naive_datetime_is_rejected(self):
        cal = CalendarRegistry.get(Market.A_SHARES)
        dt = datetime(2026, 5, 18, 2, 0)  # Naive
        with pytest.raises(ValueError, match="timezone-aware"):
            cal.is_in_session(dt)


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

    def test_calendar_failure_is_never_aggregated_as_open(self, monkeypatch):
        def unavailable_holidays(_self: MarketCalendar, _year: int) -> set[date]:
            raise CalendarUnavailableError("holiday calendar unavailable")

        monkeypatch.setattr(MarketCalendar, "_get_holidays", unavailable_holidays)
        dt = datetime(2031, 5, 19, 2, 0, tzinfo=UTC)
        assert CalendarRegistry.is_any_market_open([Market.A_SHARES], dt) is False
        assert CalendarRegistry.all_markets_closed([Market.A_SHARES], dt) is True

    def test_unregistered_configured_calendar_is_rejected_before_registry_publish(self):
        with pytest.raises(CalendarUnavailableError, match="cannot be initialized"):
            CalendarRegistry._configure_for_testing(
                SystemSettings(
                    primary_markets=[Market.OPTIONS],
                    trading_sessions=[
                        TradingSession(
                            market=Market.OPTIONS,
                            sessions=[("09:30", "11:30")],
                            timezone="Asia/Shanghai",
                            holiday_calendar="UNKNOWN_CALENDAR",
                        )
                    ],
                    market_rules=[
                        MarketRules(
                            market=Market.OPTIONS,
                            tick_size=0.01,
                            lot_size=1,
                            price_precision=2,
                            data_sources=["approved_provider"],
                            fee_model="approved_options",
                        )
                    ],
                )
            )
        assert CalendarRegistry.is_active(Market.A_SHARES) is True
        assert CalendarRegistry.is_active(Market.OPTIONS) is False

    def test_calendar_library_initialization_failure_blocks_registry_publish(self, monkeypatch):
        import pandas_market_calendars as mcal

        def initialization_failed(_code: str):
            raise RuntimeError("calendar registration missing")

        monkeypatch.setattr(mcal, "get_calendar", initialization_failed)
        with pytest.raises(CalendarUnavailableError, match="cannot be initialized"):
            CalendarRegistry._configure_for_testing(SystemSettings())
        assert CalendarRegistry.is_active(Market.A_SHARES) is True

    def test_inactive_market_cannot_bypass_registry_or_settlement_gate(self):
        CalendarRegistry._configure_for_testing(SystemSettings())
        assert CalendarRegistry.is_active(Market.FUTURES) is False
        with pytest.raises(ValueError, match="primary_markets"):
            CalendarRegistry.get(Market.FUTURES)
        with pytest.raises(ValueError, match="primary_markets"):
            CalendarRegistry.get_rules(Market.FUTURES)
        with pytest.raises(ValueError, match="primary_markets"):
            SettlementWindow.is_in_settlement_window(
                Market.FUTURES, datetime(2026, 5, 18, 7, 15, tzinfo=UTC)
            )


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

    def test_local_timezone_input_is_normalized_before_settlement_check(self):
        from zoneinfo import ZoneInfo

        local_dt = datetime(2026, 5, 18, 15, 45, tzinfo=ZoneInfo("Asia/Shanghai"))
        assert SettlementWindow.is_in_settlement_window(Market.A_SHARES, local_dt) is True


# ── Session ───────────────────────────────────────────────────────────────────


class TestSession:
    def test_contains(self):
        s = Session.from_config(("09:30", "11:30"), "morning")
        dt = datetime(2026, 5, 18, 10, 0, tzinfo=UTC)
        assert s.contains(dt) is True

    def test_not_contains_before(self):
        s = Session.from_config(("09:30", "11:30"), "morning")
        dt = datetime(2026, 5, 18, 9, 0, tzinfo=UTC)
        assert s.contains(dt) is False

    def test_not_contains_after(self):
        s = Session.from_config(("09:30", "11:30"), "morning")
        dt = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
        assert s.contains(dt) is False
