"""Tests for execution module — order management, reconciliation, order book, algorithms, fast path."""
import pytest
import time
import numpy as np
from quant_trading.execution.order_manager import (
    Order, OrderManager, OrderSide, OrderType, OrderStatus,
)
from quant_trading.execution.reconciler import PositionReconciler
from quant_trading.execution.order_book import (
    vwap_schedule, twap_schedule, pov_schedule, OrderBookSnapshot,
)
from quant_trading.execution.fast_path import FastPath, LatencyBudget


# ── Order Manager ──────────────────────────────────────────────────

class TestOrder:
    def test_create_market_order(self):
        o = Order(client_order_id="test-001", symbol="600519.SH", side=OrderSide.BUY, quantity=100)
        assert o.client_order_id == "test-001"
        assert o.symbol == "600519.SH"
        assert o.side == OrderSide.BUY
        assert o.quantity == 100
        assert o.order_type == OrderType.MARKET
        assert o.status == OrderStatus.PENDING
        assert o.filled_qty == 0.0
        assert o.price is None

    def test_create_limit_order(self):
        o = Order("lmt-1", "000001.SZ", OrderSide.SELL, 200, OrderType.LIMIT, price=50.0)
        assert o.order_type == OrderType.LIMIT
        assert o.price == 50.0

    def test_create_stop_order(self):
        o = Order("stop-1", "BTCUSDT", OrderSide.SELL, 1, OrderType.STOP, stop_price=40000)
        assert o.order_type == OrderType.STOP
        assert o.stop_price == 40000

    def test_default_timeout(self):
        o = Order("t-1", "AAPL", OrderSide.BUY, 10)
        assert o.timeout_seconds == 30


class TestOrderManager:
    def test_submit_order(self):
        om = OrderManager()
        o = Order("id-1", "600519.SH", OrderSide.BUY, 100)
        oid = om.submit(o)
        assert oid == "id-1"
        assert o.status == OrderStatus.SUBMITTED
        assert om.get("id-1") is o

    def test_submit_idempotent_client_id(self):
        """AGENTS.md §36: unique client_order_id, re-submit returns existing."""
        om = OrderManager()
        o1 = Order("dup-id", "AAPL", OrderSide.BUY, 10)
        o2 = Order("dup-id", "AAPL", OrderSide.BUY, 20)
        om.submit(o1)
        om.submit(o2)
        result = om.get("dup-id")
        assert result is not None
        assert result.quantity == 20  # overwritten

    def test_cancel_order(self):
        om = OrderManager()
        o = Order("c-1", "AAPL", OrderSide.SELL, 50)
        om.submit(o)
        assert om.cancel("c-1") is True
        assert o.status == OrderStatus.CANCELLED

    def test_cancel_nonexistent(self):
        om = OrderManager()
        assert om.cancel("no-such-id") is False

    def test_get_nonexistent(self):
        om = OrderManager()
        assert om.get("ghost") is None

    def test_get_active_filters_cancelled(self):
        om = OrderManager()
        om.submit(Order("a-1", "AAPL", OrderSide.BUY, 10))
        o2 = Order("a-2", "AAPL", OrderSide.BUY, 20)
        om.submit(o2)
        om.cancel("a-2")
        active = om.get_active()
        assert len(active) == 1
        assert active[0].client_order_id == "a-1"

    def test_get_active_includes_submitted_and_pending(self):
        om = OrderManager()
        om.submit(Order("p-1", "AAPL", OrderSide.BUY, 10))  # goes to SUBMITTED
        assert len(om.get_active()) == 1

    def test_multiple_orders(self):
        om = OrderManager()
        for i in range(5):
            om.submit(Order(f"m-{i}", "AAPL", OrderSide.BUY, 10))
        assert len(om.get_active()) == 5


# ── Position Reconciler ────────────────────────────────────────────

class TestPositionReconciler:
    def test_exact_match(self):
        pr = PositionReconciler(tolerance=0.0001)
        assert pr.reconcile(1000.0, 1000.0) is True

    def test_within_tolerance(self):
        pr = PositionReconciler(tolerance=0.001)
        assert pr.reconcile(1000.0, 1000.5) is True

    def test_outside_tolerance(self):
        pr = PositionReconciler(tolerance=0.0001)
        assert pr.reconcile(1000.0, 1002.0) is False

    def test_zero_exchange_position(self):
        """AGENTS.md §18: tolerance relative to exchange position, use max(abs, 1.0)."""
        pr = PositionReconciler(tolerance=0.01)
        assert pr.reconcile(0.1, 0.0) is False

    def test_zero_both(self):
        pr = PositionReconciler()
        assert pr.reconcile(0.0, 0.0) is True

    def test_large_positions(self):
        pr = PositionReconciler(tolerance=0.0001)
        # 1e6 position, 200 diff = 0.02% = outside 0.01% tolerance
        assert pr.reconcile(1_000_000, 1_000_200) is False
        # 1e6 position, 50 diff = 0.005% = within
        assert pr.reconcile(1_000_000, 1_000_050) is True

    def test_custom_tolerance(self):
        pr_loose = PositionReconciler(tolerance=0.15)  # 15% tolerance
        assert pr_loose.reconcile(100, 90) is True  # 10/90=11.1% < 15%
        pr_tight = PositionReconciler(tolerance=0.00001)  # 0.001% tolerance
        assert pr_tight.reconcile(1000, 1000.1) is False  # 0.1/1000.1=0.00999% > 0.001%


# ── Order Book ─────────────────────────────────────────────────────

class TestVWAPSchedule:
    def test_uniform_volume(self):
        sched = vwap_schedule(100, 5, volume_profile=np.ones(5))
        assert len(sched) == 5
        assert sched.sum() == pytest.approx(100.0, abs=0.1)

    def test_custom_profile(self):
        profile = np.array([0.1, 0.2, 0.3, 0.2, 0.2])
        sched = vwap_schedule(100, 5, volume_profile=profile)
        assert sched[0] < sched[2]  # period 2 (idx 2) has 0.3 share vs 0.1
        assert sched.sum() == pytest.approx(100.0, abs=0.5)

    def test_single_period(self):
        sched = vwap_schedule(50, 1, volume_profile=np.ones(1))
        assert len(sched) == 1
        assert sched[0] == pytest.approx(50.0)


class TestTWAPSchedule:
    def test_even_split(self):
        sched = twap_schedule(100, 4)
        assert len(sched) == 4
        assert all(x == 25.0 for x in sched)

    def test_single_period(self):
        sched = twap_schedule(75, 1)
        assert sched[0] == 75.0

    def test_rounding(self):
        sched = twap_schedule(10, 3)
        assert sched.sum() == pytest.approx(10.0, abs=0.1)


class TestPOVSchedule:
    def test_participation(self):
        """10% participation of 1000 volume = 100 max."""
        size = pov_schedule(500, 0.1, 1000)
        assert size == 100.0

    def test_capped_by_total(self):
        size = pov_schedule(50, 0.2, 500)
        assert size == 50.0

    def test_full_participation(self):
        size = pov_schedule(100, 1.0, 100)
        assert size == 100.0


class TestOrderBookSnapshot:
    def test_bids_sorted_desc(self):
        ob = OrderBookSnapshot([(99.0, 100), (100.0, 200)], [(101.0, 50)])
        assert ob.bids[0][0] == 100.0

    def test_asks_sorted_asc(self):
        ob = OrderBookSnapshot([], [(101.0, 50), (100.5, 100)])
        assert ob.asks[0][0] == 100.5

    def test_vwap_buy(self):
        ob = OrderBookSnapshot([], [(100.0, 10), (101.0, 10)])
        price = ob.vwap("buy", 15)
        # First 10 @100, next 5 @101 → (1000 + 505)/15 = 100.33
        assert price == pytest.approx(100.33, abs=0.1)

    def test_vwap_sell(self):
        ob = OrderBookSnapshot([(100.0, 10), (99.0, 10)], [])
        price = ob.vwap("sell", 15)
        assert price == pytest.approx(99.67, abs=0.1)

    def test_vwap_full_fill(self):
        ob = OrderBookSnapshot([], [(100.0, 5)])
        price = ob.vwap("buy", 5)
        assert price == 100.0

    def test_vwap_exceeds_liquidity(self):
        ob = OrderBookSnapshot([], [(100.0, 5)])
        price = ob.vwap("buy", 100)
        assert price == 100.0  # falls back to last level


# ── Fast Path ──────────────────────────────────────────────────────

class TestLatencyBudget:
    def test_defaults(self):
        lb = LatencyBudget()
        assert lb.market_data_ns == 10_000_000
        assert lb.signal_ns == 30_000_000
        assert lb.order_ns == 60_000_000

    def test_custom(self):
        lb = LatencyBudget(market_data_ns=5_000_000, order_ns=20_000_000)
        assert lb.market_data_ns == 5_000_000
        assert lb.order_ns == 20_000_000


class TestFastPath:
    def test_elapsed_positive(self):
        fp = FastPath()
        fp.start_tick()
        time.sleep(0.001)  # 1ms
        elapsed = fp.elapsed_us()
        assert elapsed > 0

    def test_check_latency_within_budget(self):
        fp = FastPath(LatencyBudget(market_data_ns=1_000_000_000))  # 1s budget
        fp.start_tick()
        assert fp.check_latency("market_data") is True

    def test_check_latency_unknown_stage(self):
        fp = FastPath()
        fp.start_tick()
        assert fp.check_latency("unknown") is True  # falls back to 60000us default

    def test_custom_budget_default(self):
        fp = FastPath()  # uses default LatencyBudget
        assert fp.budget.market_data_ns == 10_000_000
