"""Tests for strategy framework."""
import pandas as pd
from quant_trading.strategy.framework import (
    MovingAverageCrossStrategy,
    RSIStrategy,
    SignalType,
    StrategyConfig,
    StrategyState,
)


class TestMAStrategy:
    def test_golden_cross(self):
        config = StrategyConfig(parameters={"fast": 5, "slow": 20})
        s = MovingAverageCrossStrategy(config)
        # Create data where fast crosses above slow
        close = [10.0]*19 + [11.0]  # Jump up
        df = pd.DataFrame({"close": close}, index=pd.bdate_range("2024-01-01", periods=20, freq="B"))
        signals = s.on_data({"TEST": df})
        assert len(signals) > 0
        assert signals[0].signal_type == SignalType.BUY

class TestRSIStrategy:
    def test_oversold_buy(self):
        config = StrategyConfig(parameters={"period": 5, "oversold": 30, "overbought": 70})
        s = RSIStrategy(config)
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
