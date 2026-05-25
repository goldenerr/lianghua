"""Tests for market router and futures rollover."""

from datetime import date, datetime, timezone

from quant_trading.config.market_calendar import Market
from quant_trading.config.market_router import (
    ContractMonth,
    FuturesRolloverDetector,
    MarketRouter,
    MarketRuleSet,
)

UTC = timezone.utc


# ── MarketRuleSet ─────────────────────────────────────────────────────────────


class TestMarketRuleSet:
    def test_round_price_a_shares(self):
        rules = MarketRuleSet(Market.A_SHARES, tick_size=0.01)
        # 10.123 / 0.01 = 1012.3 → round(1012.3) = 1012 → 10.12
        assert abs(rules.round_price(10.123) - 10.12) < 1e-10
        # 10.125 / 0.01 = 1012.5 → banker's rounding → 1012 → 10.12
        assert abs(rules.round_price(10.125) - 10.12) < 1e-10
        # 10.126 / 0.01 = 1012.6 → round(1012.6) = 1013 → 10.13
        assert abs(rules.round_price(10.126) - 10.13) < 1e-10
        # 10.135 / 0.01 = 1013.5 → banker's rounding → 1014 → 10.14
        assert abs(rules.round_price(10.135) - 10.14) < 1e-10

    def test_round_price_precision(self):
        """Verify round_price handles float precision correctly."""
        rules = MarketRuleSet(Market.A_SHARES, tick_size=0.01)
        result = rules.round_price(10.123)
        assert abs(result - 10.12) < 1e-10

    def test_round_quantity(self):
        rules = MarketRuleSet(Market.A_SHARES, lot_size=100)
        assert rules.round_quantity(250) == 200
        assert rules.round_quantity(251) == 300  # round half up
        assert rules.round_quantity(50) == 0  # below half lot → 0

    def test_round_quantity_crypto(self):
        rules = MarketRuleSet(Market.CRYPTO, lot_size=1)
        assert rules.round_quantity(1.5) == 2
        assert rules.round_quantity(0.4) == 0


# ── MarketRouter ──────────────────────────────────────────────────────────────


class TestMarketRouter:
    def test_get_data_sources(self):
        sources = MarketRouter.get_data_sources(Market.A_SHARES)
        assert "tushare" in sources
        assert "akshare" in sources

    def test_get_data_sources_crypto(self):
        sources = MarketRouter.get_data_sources(Market.CRYPTO)
        assert "ccxt" in sources

    def test_get_fee_model(self):
        assert MarketRouter.get_fee_model(Market.A_SHARES) == "cn_stock"
        assert MarketRouter.get_fee_model(Market.CRYPTO) == "crypto_maker_taker"

    def test_get_rules(self):
        rules = MarketRouter.get_rules(Market.A_SHARES)
        assert rules.tick_size == 0.01
        assert rules.lot_size == 100

    def test_is_trading_allowed_a_shares_lunch(self):
        """A-shares during lunch break should not allow trading."""
        dt = datetime(2026, 5, 18, 4, 0, tzinfo=UTC)  # lunch
        allowed, reason = MarketRouter.is_trading_allowed(Market.A_SHARES, dt)
        assert allowed is False
        assert "session" in reason.lower()

    def test_is_trading_allowed_a_shares_morning(self):
        """A-shares during morning session should allow trading."""
        dt = datetime(2026, 5, 18, 2, 0, tzinfo=UTC)  # morning
        allowed, reason = MarketRouter.is_trading_allowed(Market.A_SHARES, dt)
        assert allowed is True

    def test_is_trading_allowed_settlement_window(self):
        """Settlement window: when NOT in session, returns 'not in session' first."""
        # A-shares afternoon session ends at 07:00 UTC, settlement window 07:30-08:00.
        # 07:45 UTC is after session close → returns "not in session" (correct).
        dt = datetime(2026, 5, 18, 7, 45, tzinfo=UTC)
        allowed, reason = MarketRouter.is_trading_allowed(
            Market.A_SHARES, dt, allow_settlement_window=False
        )
        assert allowed is False
        assert "session" in reason.lower()

    def test_is_trading_allowed_in_session_settlement(self):
        """Settlement window check: use a time inside session but near settlement.
        For A-shares, no overlap (session ends 07:00, settlement 08:00).
        Verify SettlementWindow directly instead."""
        from quant_trading.config.market_calendar import SettlementWindow

        # 07:45 UTC IS in settlement window for A-shares
        dt = datetime(2026, 5, 18, 7, 45, tzinfo=UTC)
        assert SettlementWindow.is_in_settlement_window(Market.A_SHARES, dt) is True

    def test_is_trading_allowed_weekend(self):
        """Weekend should not allow trading."""
        dt = datetime(2026, 5, 16, 2, 0, tzinfo=UTC)  # Saturday
        allowed, reason = MarketRouter.is_trading_allowed(Market.A_SHARES, dt)
        assert allowed is False

    def test_is_trading_allowed_crypto_always(self):
        """Crypto should always be allowed."""
        dt = datetime(2026, 5, 16, 2, 0, tzinfo=UTC)  # Saturday
        allowed, reason = MarketRouter.is_trading_allowed(Market.CRYPTO, dt)
        assert allowed is True


# ── ContractMonth ─────────────────────────────────────────────────────────────


class TestContractMonth:
    def test_parse(self):
        cm = ContractMonth("IF2406")
        assert cm.underlying == "IF"
        assert cm.year == 2024
        assert cm.month == 6
        assert cm.code == "IF2406"

    def test_expiry_date(self):
        cm = ContractMonth("IF2406")
        exp = cm.expiry_date
        assert exp.year == 2024
        assert exp.month == 6
        assert exp.weekday() == 4  # Friday

    def test_equality(self):
        a = ContractMonth("IF2406")
        b = ContractMonth("IF2406")
        c = ContractMonth("IF2409")
        assert a == b
        assert a != c

    def test_hashable(self):
        cm = ContractMonth("IF2406")
        d = {cm: "test"}
        assert d[cm] == "test"


# ── FuturesRolloverDetector ───────────────────────────────────────────────────


class TestFuturesRolloverDetector:
    def test_no_rollover_initially(self):
        detector = FuturesRolloverDetector("IF")
        assert detector.dominant_contract is None

    def test_first_contract_becomes_dominant(self):
        detector = FuturesRolloverDetector("IF")
        cm = ContractMonth("IF2406")
        detector.update_volume(cm, 10000)
        assert detector.dominant_contract == cm

    def test_rollover_detected(self):
        detector = FuturesRolloverDetector("IF", consecutive_days=2)

        cm1 = ContractMonth("IF2406")
        cm2 = ContractMonth("IF2409")

        # Initialize: set cm1 as dominant
        detector.update_volume(cm1, 10000)
        assert detector.dominant_contract == cm1

        # Day 1: cm2 overtakes cm1 in volume
        result = detector.check_rollover([(cm1, 5000), (cm2, 6000)], date(2024, 5, 1))
        assert result is None  # Not yet 2 consecutive days

        # Day 2: cm2 leads again → rollover
        result = detector.check_rollover([(cm1, 4000), (cm2, 7000)], date(2024, 5, 2))
        assert result is not None
        old, new = result
        assert old.code == "IF2406"
        assert new.code == "IF2409"
        assert detector.dominant_contract is not None
        assert detector.dominant_contract.code == "IF2409"

    def test_rollover_cost_estimate(self):
        detector = FuturesRolloverDetector("IF")
        old = ContractMonth("IF2406")
        new = ContractMonth("IF2409")
        cost = detector.rollover_cost_estimate(old, new, position=10, current_price=3500)
        assert "spread_cost" in cost
        assert "commission" in cost
        assert "total" in cost
        assert cost["total"] > 0
