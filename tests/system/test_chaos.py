"""Stability & chaos tests (test-002)."""

from datetime import datetime, timedelta, timezone

from quant_trading.core.state_machine import SystemState, SystemStateMachine
from quant_trading.monitor.continuity import FaultInjector
from quant_trading.monitor.monitor import SystemMonitor


def test_with_latency_injection():
    monitor = SystemMonitor()
    injector = FaultInjector(target="redis", fault_type="latency")

    injected = injector.inject(duration_ms=250)
    monitor.update(latency_ms=injected["duration_ms"])

    assert injector.active is True
    assert monitor.get()["latency_ms"] == 250
    recovered = injector.recover()
    assert recovered["recovered"] is True
    assert injector.active is False


def test_network_partition_recovery():
    fsm = SystemStateMachine()
    fsm.transition(SystemState.RUNNING)
    injector = FaultInjector(target="exchange_gateway", fault_type="network_partition")

    injector.inject(duration_ms=1_000)
    fsm.enter_safe_mode("network partition detected")
    assert fsm.state == SystemState.SAFE_MODE

    recovered = injector.recover()
    assert recovered["recovered"] is True
    fsm.transition(SystemState.RUNNING)
    assert fsm.can_trade is True


def test_time_jump_detection():
    fsm = SystemStateMachine()
    fsm.transition(SystemState.RUNNING)
    now = datetime.now(timezone.utc)
    observed = now + timedelta(seconds=120)

    drift_seconds = abs((observed - now).total_seconds())
    if drift_seconds > 30:
        fsm.enter_safe_mode(f"clock drift {drift_seconds:.0f}s")

    assert drift_seconds == 120
    assert fsm.state == SystemState.SAFE_MODE
