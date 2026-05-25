"""Tests for slippage model, impact model, and order replay."""
import pytest
from quant_trading.execution.replay import ReplayMode, ReplayResult, replay_orders
from quant_trading.execution.slippage import ImpactModel, SlippageModel


class TestSlippageModel:
    def test_default_estimate(self):
        sm = SlippageModel()
        bps = sm.estimate(volatility=0.01, order_size_pct=0.01)
        assert bps > 0
        # base=1.0 + 0.5*0.01*10000 + 0.1*0.01*100 = 1.0 + 50 + 0.1 = 51.1
        assert bps == pytest.approx(51.1, abs=0.1)

    def test_zero_vol_zero_size(self):
        sm = SlippageModel()
        bps = sm.estimate(0.0, 0.0)
        assert bps == pytest.approx(1.0)  # just base

    def test_never_negative(self):
        sm = SlippageModel(base_bps=0.0, vol_sensitivity=0.0, size_sensitivity=0.0)
        bps = sm.estimate(0.0, 0.0)
        assert bps >= 0

    def test_high_vol(self):
        sm = SlippageModel()
        bps = sm.estimate(volatility=0.05, order_size_pct=0.02)
        assert bps > 100  # should be large


class TestImpactModel:
    def test_default(self):
        im = ImpactModel()
        impact = im.estimate(volatility=0.01, order_shares=1000, adv=100000)
        assert impact > 0
        # 0.1 * 0.01 * sqrt(1000/100000) = 0.001 * sqrt(0.01) = 0.001 * 0.1 = 0.0001
        assert impact == pytest.approx(0.0001, abs=0.00001)

    def test_zero_adv(self):
        im = ImpactModel()
        assert im.estimate(0.01, 1000, 0) == 0.0

    def test_large_order(self):
        im = ImpactModel(kappa=0.2)
        impact_small = im.estimate(0.01, 100, 100000)
        impact_large = im.estimate(0.01, 10000, 100000)
        assert impact_large > impact_small


# ── Replay ──────────────────────────────────────────────────────────

class TestReplayResult:
    def test_match_rate_empty(self):
        rr = ReplayResult(mode=ReplayMode.DECISION, orders_replayed=0)
        assert rr.match_rate == 0.0

    def test_match_rate_all_match(self):
        rr = ReplayResult(mode=ReplayMode.FULL, orders_replayed=10, matches=10)
        assert rr.match_rate == 1.0

    def test_match_rate_half(self):
        rr = ReplayResult(mode=ReplayMode.ORDER_MATCHING, orders_replayed=10, matches=5, mismatches=5)
        assert rr.match_rate == 0.5


class MockEngine:
    def decide(self, order):
        return order  # always matches


class MismatchEngine:
    def decide(self, order):
        return {**order, "mismatched": True}


def test_replay_all_match():
    orders = [{"symbol": "AAPL", "qty": 10}, {"symbol": "GOOG", "qty": 5}]
    engine = MockEngine()
    result = replay_orders(orders, engine)
    assert result.orders_replayed == 2
    assert result.matches == 2
    assert result.mismatches == 0


def test_replay_mismatch():
    orders = [{"symbol": "AAPL", "qty": 10}]
    engine = MismatchEngine()
    result = replay_orders(orders, engine)
    assert result.matches == 0
    assert result.mismatches == 1
    assert result.differences[0].index == 0
    assert result.input_hash


def test_replay_no_decide_method():
    """Missing validation logic cannot be treated as a matching replay."""
    orders = [{"symbol": "AAPL", "qty": 10}]

    class NoDecideEngine:
        pass

    with pytest.raises(ValueError, match="decide"):
        replay_orders(orders, NoDecideEngine())
