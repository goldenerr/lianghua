"""Invariant violation chaos testing (test-006)."""

from quant_trading.core.state_machine import InvariantEnforcer, SystemState, SystemStateMachine


def test_position_mismatch_triggers_safe_mode():
    fsm = SystemStateMachine()
    enforcer = InvariantEnforcer(fsm)
    result = enforcer.check_position_invariant(internal=100.0, pending=0.0, exchange=120.0)
    assert result.passed is False
    assert fsm.state == SystemState.SAFE_MODE


def test_equity_mismatch_triggers_safe_mode():
    fsm = SystemStateMachine()
    enforcer = InvariantEnforcer(fsm)
    results = enforcer.check_all(
        realized_pnl=10.0,
        unrealized_pnl=10.0,
        cash=10.0,
        total_equity=1000.0,  # intentional violation
    )
    equity = next(r for r in results if r.name == "equity")
    assert equity.passed is False
    assert enforcer._violation_count.get("equity", 0) >= 1
    assert fsm.state == SystemState.SAFE_MODE
