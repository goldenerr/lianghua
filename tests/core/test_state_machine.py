"""Tests for state machine and invariant enforcement."""
import pytest
from quant_trading.core.state_machine import (
    OrderStateMachine, OrderState,
    PositionStateMachine, PositionState,
    SystemStateMachine, SystemState,
    InvariantEnforcer, InvariantCheck,
)


class TestOrderStateMachine:
    def test_valid_transitions(self):
        fsm = OrderStateMachine("ord-1")
        assert fsm.transition(OrderState.SUBMITTED) is True
        assert fsm.state == OrderState.SUBMITTED

    def test_invalid_transition_blocked(self):
        fsm = OrderStateMachine("ord-2")
        assert fsm.transition(OrderState.FILLED) is False  # Created → Filled invalid
        assert fsm.state == OrderState.CREATED

    def test_full_lifecycle(self):
        fsm = OrderStateMachine("ord-3")
        assert fsm.transition(OrderState.SUBMITTED)
        assert fsm.transition(OrderState.PARTIALLY_FILLED)
        assert fsm.transition(OrderState.FILLED)
        assert fsm.state == OrderState.FILLED
        # Filled is terminal
        assert fsm.transition(OrderState.CANCELLED) is False

    def test_history_recorded(self):
        fsm = OrderStateMachine("ord-4")
        fsm.transition(OrderState.SUBMITTED)
        fsm.transition(OrderState.FILLED)
        assert len(fsm.history) == 2
        assert fsm.history[0]["from"] == "created"
        assert fsm.history[1]["to"] == "filled"


class TestPositionStateMachine:
    def test_flat_to_long(self):
        fsm = PositionStateMachine("BTC/USDT")
        assert fsm.transition(PositionState.LONG) is True
        assert fsm.state == PositionState.LONG

    def test_long_to_short(self):
        fsm = PositionStateMachine("ETH/USDT")
        fsm.transition(PositionState.LONG)
        assert fsm.transition(PositionState.SHORT) is True


class TestSystemStateMachine:
    def test_init_state(self):
        fsm = SystemStateMachine()
        assert fsm.state == SystemState.INIT

    def test_enter_safe_mode(self):
        fsm = SystemStateMachine()
        fsm.enter_safe_mode("test violation")
        assert fsm.state == SystemState.SAFE_MODE
        assert fsm.can_trade is False

    def test_enter_emergency(self):
        fsm = SystemStateMachine()
        fsm.enter_emergency("black swan")
        assert fsm.state == SystemState.EMERGENCY

    def test_running_can_trade(self):
        fsm = SystemStateMachine()
        fsm.transition(SystemState.RUNNING)
        assert fsm.can_trade is True


class TestInvariantEnforcer:
    def test_position_invariant_passes(self):
        fsm = SystemStateMachine()
        fsm.transition(SystemState.RUNNING)
        enforcer = InvariantEnforcer(fsm)
        result = enforcer.check_position_invariant(100.0, 0.0, 100.0)
        assert result.passed is True

    def test_position_invariant_fails_and_triggers_safe_mode(self):
        fsm = SystemStateMachine()
        fsm.transition(SystemState.RUNNING)
        enforcer = InvariantEnforcer(fsm)
        result = enforcer.check_position_invariant(100.0, 0.0, 200.0)
        assert result.passed is False
        assert fsm.state == SystemState.SAFE_MODE

    def test_check_all_passes_with_valid_data(self):
        fsm = SystemStateMachine()
        fsm.transition(SystemState.RUNNING)
        enforcer = InvariantEnforcer(fsm)
        results = enforcer.check_all(
            internal_position=100, exchange_position=100,
            realized_pnl=5000, unrealized_pnl=1000, cash=94000,
            total_equity=100000, var_95=3.0, max_var=5.0,
        )
        assert all(r.passed for r in results)

    def test_check_all_fails_equity(self):
        fsm = SystemStateMachine()
        fsm.transition(SystemState.RUNNING)
        enforcer = InvariantEnforcer(fsm)
        results = enforcer.check_all(
            internal_position=100, exchange_position=100,
            realized_pnl=5000, unrealized_pnl=1000, cash=94000,
            total_equity=200000,  # MISMATCH
        )
        assert any(not r.passed for r in results)
        assert fsm.state == SystemState.SAFE_MODE

    def test_order_idempotency(self):
        fsm = SystemStateMachine()
        fsm.transition(SystemState.RUNNING)
        enforcer = InvariantEnforcer(fsm)

        r1 = enforcer.check_all(client_order_id="dup-1")
        r2 = enforcer.check_all(client_order_id="dup-1")
        assert any(not r.passed for r in r2)

    def test_violation_count_increments(self):
        fsm = SystemStateMachine()
        fsm.transition(SystemState.RUNNING)
        enforcer = InvariantEnforcer(fsm)
        enforcer.check_all(internal_position=1, exchange_position=100)
        assert enforcer._violation_count.get("position", 0) > 0
