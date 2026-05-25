"""End-to-end invariant regression (test-005)."""

from quant_trading.core.state_machine import InvariantEnforcer, SystemState, SystemStateMachine


def test_position_invariant_holds():
    fsm = SystemStateMachine()
    enforcer = InvariantEnforcer(fsm)
    results = enforcer.check_all(
        internal_position=100.0,
        pending_orders=0.0,
        exchange_position=100.0,
        realized_pnl=10.0,
        unrealized_pnl=5.0,
        cash=985.0,
        total_equity=1000.0,
        var_95=0.02,
        max_var=0.05,
    )
    assert all(r.passed for r in results)
    assert fsm.state != SystemState.SAFE_MODE


def test_equity_invariant_holds():
    fsm = SystemStateMachine()
    enforcer = InvariantEnforcer(fsm)
    results = enforcer.check_all(
        internal_position=0.0,
        pending_orders=0.0,
        exchange_position=0.0,
        realized_pnl=100.0,
        unrealized_pnl=-20.0,
        cash=920.0,
        total_equity=1000.0,
        var_95=0.01,
        max_var=0.05,
    )
    equity = next(r for r in results if r.name == "equity")
    assert equity.passed is True
    assert fsm.state != SystemState.SAFE_MODE


def test_order_idempotency():
    fsm = SystemStateMachine()
    enforcer = InvariantEnforcer(fsm)
    first = enforcer.check_all(client_order_id="dup-001")
    second = enforcer.check_all(client_order_id="dup-001")
    first_idem = next(r for r in first if r.name == "order_idempotency")
    second_idem = next(r for r in second if r.name == "order_idempotency")
    assert first_idem.passed is True
    assert second_idem.passed is False
    assert fsm.state == SystemState.SAFE_MODE
