"""Tests for market router and futures rollover."""

from datetime import date, datetime, timezone

import pytest
from quant_trading.config.market_calendar import MarketCalendar
from quant_trading.config.market_router import (
    ContractMonth,
    FuturesRolloverDetector,
    MarketRouter,
    MarketRuleSet,
)
from quant_trading.config.settings import (
    Environment,
    FuturesRolloverPolicy,
    Market,
    MarketRules,
    QuantSettings,
    SystemSettings,
    TradingSession,
)
from quant_trading.core.audit import AuditBus

UTC = timezone.utc


@pytest.fixture(autouse=True)
def configured_router() -> None:
    MarketRouter._configure_for_testing(
        SystemSettings(
            primary_markets=[Market.A_SHARES, Market.FUTURES, Market.CRYPTO],
            trading_sessions=[
                TradingSession(
                    market=Market.A_SHARES,
                    sessions=[("09:30", "11:30"), ("13:00", "15:00")],
                    timezone="Asia/Shanghai",
                    holiday_calendar="XSHG",
                ),
                TradingSession(
                    market=Market.FUTURES,
                    sessions=[("09:30", "11:30")],
                    timezone="Asia/Shanghai",
                    holiday_calendar="XSHG",
                ),
                TradingSession(market=Market.CRYPTO, sessions=[("00:00", "24:00")], timezone="UTC"),
            ],
            market_rules=[
                MarketRules(
                    market=Market.A_SHARES,
                    tick_size=0.01,
                    lot_size=100,
                    price_precision=2,
                    data_sources=["tushare", "akshare"],
                    fee_model="cn_stock",
                    settlement_time="16:00",
                ),
                MarketRules(
                    market=Market.FUTURES,
                    tick_size=1.0,
                    lot_size=1,
                    price_precision=0,
                    data_sources=["tushare"],
                    fee_model="cn_futures",
                ),
                MarketRules(
                    market=Market.CRYPTO,
                    tick_size=0.01,
                    lot_size=1,
                    price_precision=2,
                    data_sources=["ccxt", "binance"],
                    fee_model="crypto_maker_taker",
                ),
            ],
        )
    )


def configure_futures_rollover(policy: FuturesRolloverPolicy) -> None:
    MarketRouter._configure_for_testing(
        SystemSettings(
            primary_markets=[Market.FUTURES],
            trading_sessions=[
                TradingSession(
                    market=Market.FUTURES,
                    sessions=[("09:30", "11:30")],
                    timezone="Asia/Shanghai",
                    holiday_calendar="XSHG",
                )
            ],
            market_rules=[
                MarketRules(
                    market=Market.FUTURES,
                    tick_size=1.0,
                    lot_size=1,
                    price_precision=0,
                    data_sources=["tushare"],
                    fee_model="cn_futures",
                )
            ],
            futures_rollover=policy,
        )
    )


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
        assert rules.round_quantity(299) == 200  # sizing never rounds exposure upward
        assert rules.round_quantity(-299) == -200  # reductions are also bounded toward zero
        assert rules.round_quantity(50) == 0

    def test_round_quantity_crypto(self):
        rules = MarketRuleSet(Market.CRYPTO, lot_size=1)
        assert rules.round_quantity(1.5) == 1
        assert rules.round_quantity(0.4) == 0

    def test_nonfinite_quantity_is_rejected(self):
        rules = MarketRuleSet(Market.CRYPTO, lot_size=1)
        with pytest.raises(ValueError, match="finite"):
            rules.round_quantity(float("nan"))

    def test_nonpositive_or_subtick_price_is_rejected(self):
        rules = MarketRuleSet(Market.A_SHARES, tick_size=0.01)
        with pytest.raises(ValueError, match="positive"):
            rules.round_price(0)
        with pytest.raises(ValueError, match="minimum executable tick"):
            rules.round_price(0.004)


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

    def test_adapter_metadata_for_disabled_market_is_blocked(self):
        with pytest.raises(ValueError, match="primary_markets"):
            MarketRouter.get_data_sources(Market.US_STOCKS)
        with pytest.raises(ValueError, match="primary_markets"):
            MarketRouter.get_fee_model(Market.US_STOCKS)

    def test_direct_runtime_publish_cannot_forge_hash_binding(self):
        with pytest.raises(PermissionError, match="ConfigLoader"):
            MarketRouter.configure(QuantSettings(system=SystemSettings(), config_hash="forged"))

    def test_production_direct_runtime_publish_cannot_forge_approval_reference(self):
        production = SystemSettings(env=Environment.PROD, require_manual_approval=True)
        forged = QuantSettings(
            system=production, config_hash="forged", config_approval_ref="forged-approval"
        )
        with pytest.raises(PermissionError, match="ConfigLoader"):
            MarketRouter.configure(forged)

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

    def test_calendar_failure_blocks_market_decision(self, monkeypatch):
        def unavailable_holidays(_self: MarketCalendar, _year: int) -> set[date]:
            raise ValueError("holiday calendar unavailable")

        monkeypatch.setattr(MarketCalendar, "_get_holidays", unavailable_holidays)
        allowed, reason = MarketRouter.is_trading_allowed(
            Market.A_SHARES, datetime(2031, 5, 19, 2, 0, tzinfo=UTC)
        )
        assert allowed is False
        assert "holiday calendar unavailable" in reason

    def test_unenabled_market_is_blocked(self):
        allowed, reason = MarketRouter.is_trading_allowed(
            Market.US_STOCKS, datetime(2026, 5, 18, 14, 0, tzinfo=UTC)
        )
        assert allowed is False
        assert "primary_markets" in reason

    def test_loaded_session_override_controls_permission(self):
        MarketRouter._configure_for_testing(
            SystemSettings(
                primary_markets=[Market.A_SHARES],
                trading_sessions=[
                    TradingSession(
                        market=Market.A_SHARES,
                        sessions=[("10:00", "10:30")],
                        timezone="Asia/Shanghai",
                        holiday_calendar="XSHG",
                    )
                ],
                market_rules=[
                    MarketRules(
                        market=Market.A_SHARES,
                        tick_size=0.01,
                        lot_size=100,
                        price_precision=2,
                        data_sources=["tushare"],
                        fee_model="cn_stock",
                    )
                ],
            )
        )
        old_open = datetime(2026, 5, 18, 1, 45, tzinfo=UTC)  # 09:45 CST
        configured_open = datetime(2026, 5, 18, 2, 15, tzinfo=UTC)  # 10:15 CST
        assert MarketRouter.is_trading_allowed(Market.A_SHARES, old_open)[0] is False
        assert MarketRouter.is_trading_allowed(Market.A_SHARES, configured_open)[0] is True


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

    @pytest.mark.parametrize("symbol", ["IF", "IF24AA", "IF2413", "2406"])
    def test_invalid_contract_codes_are_rejected(self, symbol):
        with pytest.raises(ValueError, match="invalid futures contract"):
            ContractMonth(symbol)


# ── FuturesRolloverDetector ───────────────────────────────────────────────────


class TestFuturesRolloverDetector:
    def test_no_rollover_initially(self):
        detector = MarketRouter.create_futures_rollover_detector("IF")
        assert detector.dominant_contract is None

    def test_first_contract_becomes_dominant(self):
        detector = MarketRouter.create_futures_rollover_detector("IF")
        cm = ContractMonth("IF2406")
        detector.update_volume(cm, 10000)
        assert detector.dominant_contract == cm

    def test_rollover_detected(self):
        audit = AuditBus()
        configure_futures_rollover(FuturesRolloverPolicy(consecutive_volume_days=2))
        detector = MarketRouter.create_futures_rollover_detector("IF", audit_bus=audit)

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
        events = audit.query(event_type="futures_rollover_intent_detected")
        assert events[-1]["payload"]["execution_authorized"] is False
        assert events[-1]["payload"]["requires_manual_approval"] is True

    def test_rollover_cost_estimate(self):
        audit = AuditBus()
        configure_futures_rollover(
            FuturesRolloverPolicy(spread_cost_bps=10.0, commission_bps_per_leg=2.0)
        )
        detector = MarketRouter.create_futures_rollover_detector("IF", audit_bus=audit)
        old = ContractMonth("IF2406")
        new = ContractMonth("IF2409")
        cost = detector.rollover_cost_estimate(old, new, position=10, current_price=3500)
        assert cost == {"spread_cost": 35.0, "commission": 14.0, "total": 49.0}
        events = audit.query(event_type="futures_rollover_cost_estimated")
        assert events[-1]["payload"]["spread_cost_bps"] == 10.0
        assert events[-1]["payload"]["commission_bps_per_leg"] == 2.0

    def test_different_leading_contracts_do_not_form_consecutive_roll_signal(self):
        configure_futures_rollover(FuturesRolloverPolicy(consecutive_volume_days=2))
        detector = MarketRouter.create_futures_rollover_detector("IF")
        dominant = ContractMonth("IF2406")
        first_candidate = ContractMonth("IF2409")
        second_candidate = ContractMonth("IF2412")
        detector.update_volume(dominant, 10000)

        assert (
            detector.check_rollover([(dominant, 5000), (first_candidate, 6000)], date(2024, 5, 1))
            is None
        )
        assert (
            detector.check_rollover([(dominant, 5000), (second_candidate, 7000)], date(2024, 5, 2))
            is None
        )
        assert detector.dominant_contract == dominant

    def test_same_date_observations_cannot_satisfy_consecutive_day_threshold(self):
        audit = AuditBus()
        configure_futures_rollover(FuturesRolloverPolicy(consecutive_volume_days=2))
        detector = MarketRouter.create_futures_rollover_detector("IF", audit_bus=audit)
        dominant = ContractMonth("IF2406")
        candidate = ContractMonth("IF2409")
        detector.update_volume(dominant, 10000)

        assert (
            detector.check_rollover([(dominant, 5000), (candidate, 6000)], date(2024, 5, 1)) is None
        )
        with pytest.raises(ValueError, match="strictly increasing"):
            detector.check_rollover([(dominant, 4000), (candidate, 7000)], date(2024, 5, 1))
        assert detector.dominant_contract == dominant
        assert audit.query(event_type="futures_rollover_observation_rejected")

    def test_rollover_rejects_duplicate_or_unrelated_contract_observations(self):
        detector = MarketRouter.create_futures_rollover_detector("IF")
        dominant = ContractMonth("IF2406")
        detector.update_volume(dominant, 10000)
        with pytest.raises(ValueError, match="duplicate contracts"):
            detector.check_rollover([(dominant, 1), (dominant, 2)], date(2024, 5, 1))
        with pytest.raises(ValueError, match="underlying IF"):
            detector.check_rollover([(dominant, 1), (ContractMonth("IC2409"), 2)], date(2024, 5, 2))

    def test_rollover_observation_requires_current_contract_and_forward_expiry(self):
        detector = MarketRouter.create_futures_rollover_detector("IF")
        dominant = ContractMonth("IF2409")
        detector.update_volume(dominant, 10000)
        with pytest.raises(ValueError, match="current dominant"):
            detector.check_rollover([(ContractMonth("IF2412"), 7000)], date(2024, 5, 1))
        with pytest.raises(ValueError, match="expire after"):
            detector.check_rollover(
                [(dominant, 5000), (ContractMonth("IF2406"), 6000)], date(2024, 5, 2)
            )
        assert (
            detector.check_rollover(
                [(dominant, 5000), (ContractMonth("IF2412"), 6000)], date(2024, 5, 2)
            )
            is None
        )

    def test_rollover_rejects_invalid_volume(self):
        detector = MarketRouter.create_futures_rollover_detector("IF")
        dominant = ContractMonth("IF2406")
        detector.update_volume(dominant, 10000)
        with pytest.raises(ValueError, match="finite and non-negative"):
            detector.check_rollover(
                [(dominant, 1), (ContractMonth("IF2409"), float("nan"))],
                date(2024, 5, 1),
            )

    def test_initial_volume_rejects_unrelated_contract_or_invalid_volume(self):
        detector = MarketRouter.create_futures_rollover_detector("IF")
        with pytest.raises(ValueError, match="underlying IF"):
            detector.update_volume(ContractMonth("IC2406"), 100)
        with pytest.raises(ValueError, match="finite and non-negative"):
            detector.update_volume(ContractMonth("IF2406"), -1)
        assert detector.dominant_contract is None

    def test_rollover_rejects_nonpositive_current_price(self):
        detector = MarketRouter.create_futures_rollover_detector("IF")
        with pytest.raises(ValueError, match="positive"):
            detector.rollover_cost_estimate(
                ContractMonth("IF2406"), ContractMonth("IF2409"), position=10, current_price=0
            )

    def test_direct_detector_construction_cannot_bypass_loaded_policy(self):
        with pytest.raises(ValueError, match="configured MarketRouter"):
            FuturesRolloverDetector("IF", policy=FuturesRolloverPolicy())

    def test_rollover_factory_rejects_disabled_futures_market(self):
        MarketRouter._configure_for_testing(SystemSettings())
        with pytest.raises(ValueError, match="futures market must be enabled"):
            MarketRouter.create_futures_rollover_detector("IF")

    def test_rollover_factory_rejects_unconfigured_runtime(self, monkeypatch):
        monkeypatch.setattr(MarketRouter, "_rollover_policy", None)
        with pytest.raises(ValueError, match="must be configured"):
            MarketRouter.create_futures_rollover_detector("IF")
