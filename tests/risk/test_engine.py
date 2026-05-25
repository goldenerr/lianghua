"""Tests for risk engine."""

import numpy as np
import pytest
from quant_trading.core.audit import AuditBus
from quant_trading.core.state_machine import SystemState, SystemStateMachine
from quant_trading.execution.order_manager import Order, OrderManager, OrderSide
from quant_trading.risk.engine import RiskEngine, RiskLimits


class TestRiskEngine:
    def test_position_limit_ok(self):
        engine = RiskEngine()
        result = engine.check_position_limit(10_000, 100_000)
        assert result.passed is True

    def test_position_limit_breach(self):
        engine = RiskEngine(RiskLimits(max_position_pct=0.10))
        result = engine.check_position_limit(20_000, 100_000)
        assert result.passed is False

    def test_leverage_ok(self):
        engine = RiskEngine()
        result = engine.check_leverage(100_000, 100_000)
        assert result.passed is True

    def test_leverage_breach(self):
        engine = RiskEngine(RiskLimits(max_leverage=1.5))
        result = engine.check_leverage(200_000, 100_000)
        assert result.passed is False

    def test_daily_loss_breach(self):
        engine = RiskEngine()
        result = engine.check_daily_loss(-5000, 100_000)
        assert result.passed is False  # 5% > 2%

    def test_var_check(self):
        engine = RiskEngine()
        returns = np.random.RandomState(42).normal(0, 0.01, 252)
        result = engine.check_var(returns)
        assert bool(result.passed) is True

    def test_mdd_liquidate(self):
        engine = RiskEngine()
        result = engine.check_mdd(0.30)
        assert result.passed is False
        assert "LIQUIDATE" in result.reason

    def test_risk_check_emits_audit(self):
        audit = AuditBus()
        engine = RiskEngine(audit_bus=audit)
        _ = engine.check_position_limit(10_000, 100_000)
        events = audit.query(event_type="risk_check")
        assert len(events) >= 1
        assert events[-1]["payload"]["check"] == "position_limit"

    def test_daily_loss_breach_enters_safe_mode(self):
        fsm = SystemStateMachine()
        fsm.transition(SystemState.RUNNING)
        engine = RiskEngine(system_fsm=fsm)
        result = engine.check_daily_loss(-2_500, 100_000)
        assert result.passed is False
        assert fsm.state == SystemState.SAFE_MODE
        assert "Daily loss" in fsm.history[-1]["metadata"]["reason"]

    def test_daily_loss_force_enters_emergency(self):
        fsm = SystemStateMachine()
        fsm.transition(SystemState.RUNNING)
        engine = RiskEngine(system_fsm=fsm)
        result = engine.check_daily_loss(-6_000, 100_000)
        assert result.passed is False
        assert fsm.state == SystemState.EMERGENCY

    def test_var_breach_enters_safe_mode(self):
        fsm = SystemStateMachine()
        fsm.transition(SystemState.RUNNING)
        engine = RiskEngine(RiskLimits(max_var_pct=0.01), system_fsm=fsm)
        returns = np.array([-0.05] * 20 + [0.01] * 232)
        result = engine.check_var(returns)
        assert bool(result.passed) is False
        assert fsm.state == SystemState.SAFE_MODE

    def test_mdd_liquidate_enters_emergency_and_blocks_order(self):
        fsm = SystemStateMachine()
        fsm.transition(SystemState.RUNNING)
        engine = RiskEngine(system_fsm=fsm)
        result = engine.check_mdd(0.30)
        assert result.passed is False
        assert fsm.state == SystemState.EMERGENCY

        om = OrderManager(system_fsm=fsm)
        with pytest.raises(RuntimeError, match="system state"):
            om.submit(Order("risk-block-1", "AAPL", OrderSide.BUY, 1))
