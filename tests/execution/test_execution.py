"""Tests for execution module — order management, reconciliation, order book, algorithms, fast path."""

import asyncio
import time
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest
from quant_trading.config.market_calendar import MarketCalendar
from quant_trading.config.market_router import MarketRouter
from quant_trading.config.settings import Market, MarketRules, SystemSettings, TradingSession
from quant_trading.core.audit import AuditBus
from quant_trading.core.state_machine import SystemState, SystemStateMachine
from quant_trading.execution.fast_path import FastPath, LatencyBudget
from quant_trading.execution.order_book import (
    OrderBookSnapshot,
    pov_schedule,
    twap_schedule,
    vwap_schedule,
)
from quant_trading.execution.order_manager import (
    InMemoryOrderStore,
    Order,
    OrderManager,
    OrderSide,
    OrderStatus,
    OrderType,
    ReconciledPositionSnapshot,
    ReconciledReduceOnlyValidator,
    SqliteOrderStore,
)
from quant_trading.execution.reconciler import PositionReconciler

UTC = timezone.utc


def _running_order_manager(audit_bus=None, order_store=None):
    fsm = SystemStateMachine()
    fsm.transition(SystemState.RUNNING)
    MarketRouter._configure_for_testing(SystemSettings())
    return OrderManager(
        order_store=order_store or InMemoryOrderStore(),
        audit_bus=audit_bus,
        system_fsm=fsm,
        decision_clock=lambda: datetime(2026, 5, 18, 2, 0, tzinfo=UTC),
    )


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
    def test_requires_order_store(self):
        with pytest.raises(ValueError, match="order_store is required"):
            OrderManager()

    def test_pending_intent_is_recovered_without_resubmit(self, tmp_path):
        path = tmp_path / "pending-orders.sqlite3"
        store = SqliteOrderStore(path)
        pending = Order(
            "pending-id",
            "600519.SH",
            OrderSide.BUY,
            100,
            market=Market.A_SHARES,
        )
        assert store.reserve(pending) is True

        restarted = _running_order_manager(order_store=SqliteOrderStore(path))
        assert (
            restarted.submit(
                Order(
                    "pending-id",
                    "600519.SH",
                    OrderSide.BUY,
                    100,
                    market=Market.A_SHARES,
                )
            )
            == "pending-id"
        )
        recovered = restarted.get("pending-id")
        assert recovered is not None
        assert recovered.status == OrderStatus.PENDING

    def test_submit_order(self):
        om = _running_order_manager()
        o = Order("id-1", "600519.SH", OrderSide.BUY, 100, market=Market.A_SHARES)
        oid = om.submit(o)
        assert oid == "id-1"
        assert o.status == OrderStatus.SUBMITTED
        assert om.get("id-1") is o

    def test_submit_idempotent_client_id(self):
        """AGENTS.md §36: exact replay returns the persisted original."""
        om = _running_order_manager()
        o1 = Order("dup-id", "600519.SH", OrderSide.BUY, 100, market=Market.A_SHARES)
        o2 = Order("dup-id", "600519.SH", OrderSide.BUY, 100, market=Market.A_SHARES)
        om.submit(o1)
        assert om.submit(o2) == "dup-id"
        result = om.get("dup-id")
        assert result is not None
        assert result.quantity == 100

    def test_conflicting_idempotency_key_is_rejected(self):
        audit = AuditBus()
        om = _running_order_manager(audit_bus=audit)
        om.submit(Order("dup-id", "600519.SH", OrderSide.BUY, 100, market=Market.A_SHARES))

        with pytest.raises(RuntimeError, match="idempotency conflict"):
            om.submit(Order("dup-id", "600519.SH", OrderSide.BUY, 200, market=Market.A_SHARES))

        event = audit.query("order_idempotency_conflict")[-1]["payload"]
        assert event["client_order_id"] == "dup-id"

    def test_sqlite_store_recovers_order_after_restart(self, tmp_path):
        path = tmp_path / "orders.sqlite3"
        first = _running_order_manager(order_store=SqliteOrderStore(path))
        first.submit(Order("restart-id", "600519.SH", OrderSide.BUY, 100, market=Market.A_SHARES))

        restarted = _running_order_manager(order_store=SqliteOrderStore(path))
        recovered = restarted.get("restart-id")
        assert recovered is not None
        assert recovered.status == OrderStatus.SUBMITTED
        assert recovered.quantity == 100
        assert (
            restarted.submit(
                Order("restart-id", "600519.SH", OrderSide.BUY, 100, market=Market.A_SHARES)
            )
            == "restart-id"
        )

    def test_order_manager_emits_audit_events(self):
        audit = AuditBus()
        om = _running_order_manager(audit_bus=audit)
        o = Order("evt-1", "600519.SH", OrderSide.BUY, 100, market=Market.A_SHARES)
        om.submit(o)
        om.cancel("evt-1")
        events = audit.query(limit=10)
        types = [e["event_type"] for e in events]
        assert "order_submitted" in types
        assert "order_cancelled" in types

    def test_cancel_order(self):
        om = _running_order_manager()
        o = Order("c-1", "600519.SH", OrderSide.SELL, 100, market=Market.A_SHARES)
        om.submit(o)
        assert om.cancel("c-1") is True
        assert o.status == OrderStatus.CANCELLED

    def test_cancel_nonexistent(self):
        om = _running_order_manager()
        assert om.cancel("no-such-id") is False

    def test_get_nonexistent(self):
        om = _running_order_manager()
        assert om.get("ghost") is None

    def test_get_active_filters_cancelled(self):
        om = _running_order_manager()
        om.submit(Order("a-1", "600519.SH", OrderSide.BUY, 100, market=Market.A_SHARES))
        o2 = Order("a-2", "600519.SH", OrderSide.BUY, 200, market=Market.A_SHARES)
        om.submit(o2)
        om.cancel("a-2")
        active = om.get_active()
        assert len(active) == 1
        assert active[0].client_order_id == "a-1"

    def test_get_active_includes_submitted_and_pending(self):
        om = _running_order_manager()
        om.submit(
            Order("p-1", "600519.SH", OrderSide.BUY, 100, market=Market.A_SHARES)
        )  # goes to SUBMITTED
        assert len(om.get_active()) == 1

    def test_multiple_orders(self):
        om = _running_order_manager()
        for i in range(5):
            om.submit(Order(f"m-{i}", "600519.SH", OrderSide.BUY, 100, market=Market.A_SHARES))
        assert len(om.get_active()) == 5

    def test_order_blocked_when_system_not_running(self):
        audit = AuditBus()
        om = OrderManager(order_store=InMemoryOrderStore(), audit_bus=audit)  # INIT blocks trading.
        with pytest.raises(RuntimeError, match="blocked"):
            om.submit(Order("blk-1", "AAPL", OrderSide.BUY, 1))
        events = audit.query(event_type="order_rejected_system_state")
        assert len(events) == 1
        assert events[0]["payload"]["system_state"] == SystemState.INIT.value

    def test_order_allowed_when_system_running(self):
        fsm = SystemStateMachine()
        fsm.transition(SystemState.RUNNING)
        MarketRouter._configure_for_testing(SystemSettings())
        om = OrderManager(
            order_store=InMemoryOrderStore(),
            system_fsm=fsm,
            decision_clock=lambda: datetime(2026, 5, 18, 2, 0, tzinfo=UTC),
        )
        oid = om.submit(Order("run-1", "600519.SH", OrderSide.BUY, 100, market=Market.A_SHARES))
        assert oid == "run-1"

    def test_order_quantity_must_match_configured_lot_size(self):
        audit = AuditBus()
        om = _running_order_manager(audit_bus=audit)
        with pytest.raises(RuntimeError, match="lot size 100"):
            om.submit(Order("odd-lot", "600519.SH", OrderSide.BUY, 101, market=Market.A_SHARES))
        events = audit.query(event_type="order_rejected_market_rule")
        assert events[-1]["payload"]["client_order_id"] == "odd-lot"

    @pytest.mark.parametrize(
        ("order", "expected_reason"),
        [
            (
                Order(
                    "limit-missing",
                    "600519.SH",
                    OrderSide.BUY,
                    100,
                    OrderType.LIMIT,
                    market=Market.A_SHARES,
                ),
                "requires price only",
            ),
            (
                Order(
                    "limit-tick",
                    "600519.SH",
                    OrderSide.BUY,
                    100,
                    OrderType.LIMIT,
                    price=10.005,
                    market=Market.A_SHARES,
                ),
                "tick size",
            ),
            (
                Order(
                    "market-price",
                    "600519.SH",
                    OrderSide.BUY,
                    100,
                    price=10.01,
                    market=Market.A_SHARES,
                ),
                "must not contain price",
            ),
        ],
    )
    def test_invalid_price_fields_are_rejected_before_submission(self, order, expected_reason):
        om = _running_order_manager()
        with pytest.raises(RuntimeError, match=expected_reason):
            om.submit(order)

    def test_aligned_limit_and_stop_orders_pass_market_rule_gate(self):
        om = _running_order_manager()
        assert (
            om.submit(
                Order(
                    "valid-limit",
                    "600519.SH",
                    OrderSide.BUY,
                    100,
                    OrderType.LIMIT,
                    price=10.01,
                    market=Market.A_SHARES,
                )
            )
            == "valid-limit"
        )
        assert (
            om.submit(
                Order(
                    "valid-stop",
                    "600519.SH",
                    OrderSide.SELL,
                    100,
                    OrderType.STOP,
                    stop_price=9.99,
                    market=Market.A_SHARES,
                )
            )
            == "valid-stop"
        )

    def test_order_without_market_is_rejected_at_submission_boundary(self):
        audit = AuditBus()
        om = _running_order_manager(audit_bus=audit)
        with pytest.raises(RuntimeError, match="market is required"):
            om.submit(Order("no-market", "600519.SH", OrderSide.BUY, 1))
        events = audit.query(event_type="order_rejected_market_schedule")
        assert events[-1]["payload"]["market"] is None

    def test_order_outside_session_is_rejected_and_audited(self):
        audit = AuditBus()
        fsm = SystemStateMachine()
        fsm.transition(SystemState.RUNNING)
        MarketRouter._configure_for_testing(SystemSettings())
        om = OrderManager(
            order_store=InMemoryOrderStore(),
            audit_bus=audit,
            system_fsm=fsm,
            decision_clock=lambda: datetime(2026, 5, 18, 4, 0, tzinfo=UTC),
        )
        with pytest.raises(RuntimeError, match="not in trading session"):
            om.submit(Order("lunch", "600519.SH", OrderSide.BUY, 1, market=Market.A_SHARES))
        assert len(audit.query(event_type="order_rejected_market_schedule")) == 1

    def test_unzoned_submission_time_is_rejected_and_audited(self):
        audit = AuditBus()
        fsm = SystemStateMachine()
        fsm.transition(SystemState.RUNNING)
        MarketRouter._configure_for_testing(SystemSettings())
        om = OrderManager(
            order_store=InMemoryOrderStore(),
            audit_bus=audit,
            system_fsm=fsm,
            decision_clock=lambda: datetime(2026, 5, 18, 2, 0),
        )
        with pytest.raises(RuntimeError, match="timezone-aware"):
            om.submit(Order("naive-ts", "600519.SH", OrderSide.BUY, 1, market=Market.A_SHARES))
        assert len(audit.query(event_type="order_rejected_market_schedule")) == 1

    def test_unavailable_holiday_calendar_rejects_order_and_is_audited(self, monkeypatch):
        def unavailable_holidays(_self: MarketCalendar, _year: int) -> set[date]:
            raise ValueError("holiday calendar unavailable")

        monkeypatch.setattr(MarketCalendar, "_get_holidays", unavailable_holidays)
        audit = AuditBus()
        om = _running_order_manager(audit_bus=audit)
        with pytest.raises(RuntimeError, match="holiday calendar unavailable"):
            om.submit(Order("calendar-down", "600519.SH", OrderSide.BUY, 1, market=Market.A_SHARES))
        events = audit.query(event_type="order_rejected_market_schedule")
        assert events[-1]["payload"]["reason"] == "holiday calendar unavailable"

    def test_settlement_window_only_allows_reduce_only_orders(self):
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
                        settlement_window_minutes=30,
                    )
                ],
            )
        )
        fsm = SystemStateMachine()
        fsm.transition(SystemState.RUNNING)
        om = OrderManager(
            order_store=InMemoryOrderStore(),
            system_fsm=fsm,
            decision_clock=lambda: datetime(2026, 5, 18, 7, 45, tzinfo=UTC),
            reduce_only_validator=lambda order: order.side == OrderSide.SELL,
        )
        with pytest.raises(RuntimeError, match="settlement window"):
            om.submit(Order("open-risk", "600519.SH", OrderSide.BUY, 1, market=Market.A_SHARES))
        oid = om.submit(
            Order(
                "reduce-risk",
                "600519.SH",
                OrderSide.SELL,
                100,
                market=Market.A_SHARES,
                reduce_only=True,
            )
        )
        assert oid == "reduce-risk"

    def test_settlement_reduce_only_uses_reconciled_position_validator(self):
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
                        settlement_window_minutes=30,
                    )
                ],
            )
        )
        decision_time = datetime(2026, 5, 18, 7, 45, tzinfo=UTC)
        fsm = SystemStateMachine()
        fsm.transition(SystemState.RUNNING)
        validator = ReconciledReduceOnlyValidator(
            lambda _order: ReconciledPositionSnapshot(quantity=200, as_of=decision_time),
            clock=lambda: decision_time,
        )
        om = OrderManager(
            order_store=InMemoryOrderStore(),
            system_fsm=fsm,
            decision_clock=lambda: decision_time,
            reduce_only_validator=validator,
        )

        assert (
            om.submit(
                Order(
                    "reduce-reconciled",
                    "600519.SH",
                    OrderSide.SELL,
                    100,
                    market=Market.A_SHARES,
                    reduce_only=True,
                )
            )
            == "reduce-reconciled"
        )
        with pytest.raises(RuntimeError, match="not_reducing_reconciled_position"):
            om.submit(
                Order(
                    "would-increase",
                    "600519.SH",
                    OrderSide.BUY,
                    100,
                    market=Market.A_SHARES,
                    reduce_only=True,
                )
            )
        with pytest.raises(RuntimeError, match="not_reducing_reconciled_position"):
            om.submit(
                Order(
                    "would-flip",
                    "600519.SH",
                    OrderSide.SELL,
                    300,
                    market=Market.A_SHARES,
                    reduce_only=True,
                )
            )

    def test_stale_reconciled_position_blocks_reduce_only_settlement_order(self):
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
        decision_time = datetime(2026, 5, 18, 7, 45, tzinfo=UTC)
        audit = AuditBus()
        fsm = SystemStateMachine()
        fsm.transition(SystemState.RUNNING)
        validator = ReconciledReduceOnlyValidator(
            lambda _order: ReconciledPositionSnapshot(
                quantity=100, as_of=decision_time - timedelta(seconds=31)
            ),
            clock=lambda: decision_time,
        )
        om = OrderManager(
            order_store=InMemoryOrderStore(),
            audit_bus=audit,
            system_fsm=fsm,
            decision_clock=lambda: decision_time,
            reduce_only_validator=validator,
        )

        with pytest.raises(RuntimeError, match="validator failed"):
            om.submit(
                Order(
                    "stale-reduce",
                    "600519.SH",
                    OrderSide.SELL,
                    100,
                    market=Market.A_SHARES,
                    reduce_only=True,
                )
            )
        event = audit.query("order_reduce_only_settlement_rejected")[-1]["payload"]
        assert event["reason"] == "validator_error"
        assert event["error_type"] == "ValueError"

    def test_unverified_reduce_only_order_cannot_bypass_settlement_window(self):
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
        fsm = SystemStateMachine()
        fsm.transition(SystemState.RUNNING)
        audit = AuditBus()
        om = OrderManager(
            order_store=InMemoryOrderStore(),
            audit_bus=audit,
            system_fsm=fsm,
            decision_clock=lambda: datetime(2026, 5, 18, 7, 45, tzinfo=UTC),
        )
        with pytest.raises(RuntimeError, match="validator_missing"):
            om.submit(
                Order(
                    "forged-reduce",
                    "600519.SH",
                    OrderSide.SELL,
                    1,
                    market=Market.A_SHARES,
                    reduce_only=True,
                )
            )
        event = audit.query("order_reduce_only_settlement_rejected")[-1]["payload"]
        assert event["reason"] == "validator_missing"


# ── Position Reconciler ────────────────────────────────────────────


class TestPositionReconciler:
    def test_mismatch_always_enters_safe_mode(self):
        fsm = SystemStateMachine()
        fsm.transition(SystemState.RUNNING)
        pr = PositionReconciler(system_fsm=fsm, tolerance=0.0001)

        assert pr.reconcile(1000.0, 1002.0) is False
        assert fsm.state == SystemState.SAFE_MODE
        assert fsm.can_trade is False

    def test_retry_fetch_failure_enters_safe_mode_and_propagates(self):
        fsm = SystemStateMachine()
        fsm.transition(SystemState.RUNNING)
        pr = PositionReconciler(system_fsm=fsm)

        async def fetch_internal():
            raise ConnectionError("position store unavailable")

        async def fetch_exchange():
            return 100.0

        with pytest.raises(ConnectionError, match="position store unavailable"):
            asyncio.run(
                pr.reconcile_with_retry(
                    fetch_internal, fetch_exchange, max_retries=1, backoff_seconds=(0.0,)
                )
            )
        assert fsm.state == SystemState.SAFE_MODE
        assert fsm.can_trade is False

    def test_exact_match(self):
        pr = PositionReconciler(SystemStateMachine(), tolerance=0.0001)
        assert pr.reconcile(1000.0, 1000.0) is True

    def test_within_tolerance(self):
        pr = PositionReconciler(SystemStateMachine(), tolerance=0.001)
        assert pr.reconcile(1000.0, 1000.5) is True

    def test_outside_tolerance(self):
        pr = PositionReconciler(SystemStateMachine(), tolerance=0.0001)
        assert pr.reconcile(1000.0, 1002.0) is False

    def test_zero_exchange_position(self):
        """AGENTS.md §18: tolerance relative to exchange position, use max(abs, 1.0)."""
        pr = PositionReconciler(SystemStateMachine(), tolerance=0.01)
        assert pr.reconcile(0.1, 0.0) is False

    def test_zero_both(self):
        pr = PositionReconciler(SystemStateMachine())
        assert pr.reconcile(0.0, 0.0) is True

    def test_large_positions(self):
        pr = PositionReconciler(SystemStateMachine(), tolerance=0.0001)
        # 1e6 position, 200 diff = 0.02% = outside 0.01% tolerance
        assert pr.reconcile(1_000_000, 1_000_200) is False
        # 1e6 position, 50 diff = 0.005% = within
        assert pr.reconcile(1_000_000, 1_000_050) is True

    def test_custom_tolerance(self):
        pr_loose = PositionReconciler(SystemStateMachine(), tolerance=0.15)  # 15% tolerance
        assert pr_loose.reconcile(100, 90) is True  # 10/90=11.1% < 15%
        pr_tight = PositionReconciler(SystemStateMachine(), tolerance=0.00001)  # 0.001% tolerance
        assert pr_tight.reconcile(1000, 1000.1) is False  # 0.1/1000.1=0.00999% > 0.001%

    def test_reconcile_with_retry_succeeds_after_retry(self):
        fsm = SystemStateMachine()
        fsm.transition(SystemState.RUNNING)
        pr = PositionReconciler(fsm, tolerance=0.001)
        pairs = [(100.0, 101.0), (100.0, 100.0)]
        state = {"idx": 0}

        async def fetch_internal():
            i, _ = pairs[state["idx"]]
            return i

        async def fetch_exchange():
            _, e = pairs[state["idx"]]
            state["idx"] += 1
            return e

        # Use tiny backoff for test speed
        result = asyncio.run(
            pr.reconcile_with_retry(
                fetch_internal, fetch_exchange, max_retries=2, backoff_seconds=(0.0, 0.0)
            )
        )
        assert result.matched is True
        assert result.attempts == 2
        assert fsm.state == SystemState.RUNNING
        assert fsm.can_trade is True

    def test_reconcile_with_retry_failure_enters_safe_mode(self):
        fsm = SystemStateMachine()
        fsm.transition(SystemState.RUNNING)
        pr = PositionReconciler(fsm, tolerance=0.00001)

        async def fetch_internal():
            return 100.0

        async def fetch_exchange():
            return 110.0

        result = asyncio.run(
            pr.reconcile_with_retry(
                fetch_internal, fetch_exchange, max_retries=2, backoff_seconds=(0.0, 0.0)
            )
        )
        assert result.matched is False
        assert fsm.state == SystemState.SAFE_MODE
        assert fsm.can_trade is False


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
