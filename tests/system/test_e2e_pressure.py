"""End-to-end Kill Switch + DR joint stress test (test-010)."""

import pytest
from quant_trading.core.state_machine import SystemState, SystemStateMachine
from quant_trading.execution.order_manager import Order, OrderManager, OrderSide
from quant_trading.execution.reconciler import PositionReconciler
from quant_trading.operations import BackupManager
from quant_trading.operations_mr import FailoverManager, Region
from quant_trading.risk.advanced import KillSwitch


def test_e2e_kill_switch_pressure(tmp_path):
    ks = KillSwitch()
    bm = BackupManager(storage_dir=tmp_path)

    _ = bm.backup("system_state")
    ks.activate("pressure drill: daily_loss>5%")

    assert ks.is_active is True
    assert bm.verify()["passed"] is True


def test_cross_region_sync():
    fm = FailoverManager()
    assert fm.active == Region.PRIMARY
    assert fm.failover(Region.SECONDARY) is True
    status = fm.sync_status()
    assert status["active"] == Region.SECONDARY.value
    assert status["lag_seconds"] <= 1.0


def test_failover_rejects_unhealthy_region():
    fm = FailoverManager()
    fm.set_region_health(Region.SECONDARY, False)
    assert fm.failover(Region.SECONDARY) is False
    assert fm.active == Region.PRIMARY


def test_reconcile_violation_blocks_new_orders():
    fsm = SystemStateMachine()
    fsm.transition(SystemState.RUNNING)
    pr = PositionReconciler(tolerance=0.0001, on_safe_mode=fsm.enter_safe_mode)

    assert pr.reconcile(100.0, 130.0) is False
    assert fsm.state == SystemState.SAFE_MODE

    om = OrderManager(system_fsm=fsm)
    with pytest.raises(RuntimeError, match="system state"):
        om.submit(Order("safe-block-1", "AAPL", OrderSide.BUY, 1))
