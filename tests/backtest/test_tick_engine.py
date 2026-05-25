"""Tests for tick-level backtesting and execution accounting."""

import numpy as np
from quant_trading.backtest.tick_engine import TickBacktestEngine, TickGenerator


def test_tick_generator_respects_daily_price_bounds():
    generator = TickGenerator(tick_frequency_sec=60, spread_bps=2)
    session = generator.generate_session(100.0, 105.0, 95.0, 102.0, 100_000, "2026-01-02")
    prices = np.array([tick.price for tick in session.ticks])

    assert len(session.ticks) > 0
    assert prices.min() >= 95.0 - 1e-8
    assert prices.max() <= 105.0 + 1e-8
    assert len(session.bars[60]) > 0
    assert session.ticks[0].ask > session.ticks[0].bid


def test_tick_backtest_marks_bought_positions_to_market():
    engine = TickBacktestEngine(TickGenerator(tick_frequency_sec=60))
    daily_data = {
        "TEST": {
            "open": np.array([100.0, 101.0]),
            "high": np.array([102.0, 103.0]),
            "low": np.array([99.0, 100.0]),
            "close": np.array([101.0, 102.0]),
            "volume": np.array([1_000_000, 1_000_000]),
        }
    }

    def hold_half(_day_idx, _symbols):
        return {"TEST": 0.5}

    result = engine.run_backtest(daily_data, hold_half, ["d1", "d2"], initial_capital=1_000_000)
    assert result.n_ticks_processed > 0
    assert result.n_trades > 0
    # Building a 50% position should not be mistaken for losing roughly 50% cash.
    assert result.total_return > -0.10
    assert 0.0 <= result.fill_rate <= 1.0
