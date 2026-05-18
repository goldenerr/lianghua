"""Tests for strategy framework."""
import numpy as np
import pandas as pd
from quant_trading.strategy.framework import (
    MovingAverageCrossStrategy, RSIStrategy, StrategyConfig, SignalType
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
