"""Tests for strategy framework."""

from datetime import datetime, timezone

import pandas as pd
from quant_trading.config.market_router import MarketRouter
from quant_trading.config.settings import Market, MarketRules, SystemSettings, TradingSession
from quant_trading.core.audit import AuditBus
from quant_trading.strategy.framework import (
    MovingAverageCrossStrategy,
    RSIStrategy,
    SignalType,
    StrategyConfig,
    StrategyState,
)

UTC = timezone.utc


def _during_a_share_session() -> datetime:
    return datetime(2026, 5, 18, 2, 0, tzinfo=UTC)


def _configured_strategy(strategy_cls, config, audit_bus=None):
    MarketRouter._configure_for_testing(SystemSettings())
    return strategy_cls(config, audit_bus=audit_bus, decision_clock=_during_a_share_session)


class TestMAStrategy:
    def test_golden_cross(self):
        config = StrategyConfig(market=Market.A_SHARES, parameters={"fast": 5, "slow": 20})
        s = _configured_strategy(MovingAverageCrossStrategy, config)
        # Create data where fast crosses above slow
        close = [10.0] * 19 + [11.0]  # Jump up
        df = pd.DataFrame(
            {"close": close}, index=pd.bdate_range("2024-01-01", periods=20, freq="B")
        )
        signals = s.on_data({"TEST": df})
        assert len(signals) > 0
        assert signals[0].signal_type == SignalType.BUY


class TestRSIStrategy:
    def test_oversold_buy(self):
        config = StrategyConfig(
            market=Market.A_SHARES,
            parameters={"period": 5, "oversold": 30, "overbought": 70},
        )
        s = _configured_strategy(RSIStrategy, config)
        # Create data with steep decline (should trigger oversold)
        close = [100.0, 95.0, 90.0, 85.0, 80.0, 75.0]
        df = pd.DataFrame({"close": close}, index=pd.bdate_range("2024-01-01", periods=6, freq="B"))
        signals = s.on_data({"TEST": df})
        if signals:
            assert signals[0].signal_type == SignalType.BUY


class TestStrategyLifecycleEvents:
    def test_order_fill_and_risk_events_are_recorded(self):
        config = StrategyConfig(name="audit_strategy")
        s = MovingAverageCrossStrategy(config)

        s.on_order({"client_order_id": "o-1", "symbol": "AAPL"})
        s.on_fill({"client_order_id": "o-1", "filled_qty": 10})
        s.on_risk({"level": "critical", "reason": "var breach"})

        history = s.event_history()
        assert history["orders"][0]["client_order_id"] == "o-1"
        assert history["fills"][0]["filled_qty"] == 10
        assert history["risk_events"][0]["reason"] == "var breach"
        assert s.state == StrategyState.PAUSED


class TestMarketScheduleSignalGate:
    @staticmethod
    def _cross_data() -> dict[str, pd.DataFrame]:
        return {
            "600519.SH": pd.DataFrame(
                {"close": [10.0] * 19 + [11.0]},
                index=pd.bdate_range("2024-01-01", periods=20, freq="B"),
            )
        }

    def test_strategy_without_market_does_not_emit_signals(self):
        audit = AuditBus()
        s = _configured_strategy(
            MovingAverageCrossStrategy,
            StrategyConfig(parameters={"fast": 5, "slow": 20}),
            audit,
        )
        assert s.on_data(self._cross_data()) == []
        events = audit.query(event_type="strategy_signal_suppressed_market_schedule")
        assert events[-1]["payload"]["market"] is None

    def test_closed_market_signal_is_suppressed_and_audited(self):
        audit = AuditBus()
        MarketRouter._configure_for_testing(SystemSettings())
        s = MovingAverageCrossStrategy(
            StrategyConfig(market=Market.A_SHARES, parameters={"fast": 5, "slow": 20}),
            audit_bus=audit,
            decision_clock=lambda: datetime(2026, 5, 18, 4, 0, tzinfo=UTC),
        )
        assert s.on_data(self._cross_data()) == []
        events = audit.query(event_type="strategy_signal_suppressed_market_schedule")
        assert "not in trading session" in events[-1]["payload"]["reason"]

    def test_unzoned_strategy_time_is_suppressed_and_audited(self):
        audit = AuditBus()
        MarketRouter._configure_for_testing(SystemSettings())
        s = MovingAverageCrossStrategy(
            StrategyConfig(market=Market.A_SHARES, parameters={"fast": 5, "slow": 20}),
            audit_bus=audit,
            decision_clock=lambda: datetime(2026, 5, 18, 2, 0),
        )
        assert s.on_data(self._cross_data()) == []
        events = audit.query(event_type="strategy_signal_suppressed_market_schedule")
        assert "timezone-aware" in events[-1]["payload"]["reason"]

    def test_settlement_window_signal_is_suppressed(self):
        MarketRouter._configure_for_testing(
            SystemSettings(
                primary_markets=[Market.A_SHARES],
                trading_sessions=[
                    TradingSession(
                        market=Market.A_SHARES,
                        sessions=[("09:30", "16:00")],
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
                        settlement_time="16:00",
                    )
                ],
            )
        )
        s = MovingAverageCrossStrategy(
            StrategyConfig(market=Market.A_SHARES, parameters={"fast": 5, "slow": 20}),
            decision_clock=lambda: datetime(2026, 5, 18, 7, 45, tzinfo=UTC),
        )
        assert s.on_data(self._cross_data()) == []
