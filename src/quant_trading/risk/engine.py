"""
Multi-layered risk management system (risk-001).

AGENTS.md §6: Pre-trade, in-trade, post-trade risk checks.
"""

from dataclasses import dataclass, field

import numpy as np

from quant_trading.core.audit import AuditBus
from quant_trading.core.state_machine import SystemState, SystemStateMachine


@dataclass
class RiskLimits:
    max_position_pct: float = 0.20
    max_leverage: float = 2.0
    max_daily_loss_pct: float = 0.02
    max_single_loss_pct: float = 0.005
    var_confidence: float = 0.95
    max_var_pct: float = 0.05
    mdd_reduce: float = 0.15
    mdd_liquidate: float = 0.25
    daily_cb: float = 0.03
    daily_cb_force: float = 0.05
    max_volume_pct: float = 0.10
    max_consecutive_losses: int = 5
    max_weekly_loss_pct: float = 0.03


@dataclass
class RiskCheckResult:
    passed: bool
    reason: str = ""
    limits: dict = field(default_factory=dict)


class RiskEngine:
    """Pre/in/post trade risk management."""

    def __init__(
        self,
        limits: RiskLimits | None = None,
        audit_bus: AuditBus | None = None,
        system_fsm: SystemStateMachine | None = None,
    ):
        self.limits = limits or RiskLimits()
        self._daily_pnl: float = 0.0
        self._consecutive_losses: int = 0
        self._audit = audit_bus
        self._system_fsm = system_fsm

    def _emit(self, check_name: str, result: RiskCheckResult, extra: dict | None = None) -> None:
        if not self._audit:
            return
        payload = {
            "check": check_name,
            "passed": result.passed,
            "reason": result.reason,
            "limits": result.limits,
        }
        if extra:
            payload.update(extra)
        self._audit.record("risk_check", "risk_engine", payload)

    def _escalate(self, target_state: SystemState, reason: str) -> None:
        if not self._system_fsm:
            return
        if target_state == SystemState.EMERGENCY:
            self._system_fsm.enter_emergency(reason)
        elif target_state == SystemState.SAFE_MODE:
            self._system_fsm.enter_safe_mode(reason)

    def check_position_limit(self, position_value: float, total_equity: float) -> RiskCheckResult:
        pct = abs(position_value) / max(total_equity, 1.0)
        ok = pct <= self.limits.max_position_pct
        result = RiskCheckResult(
            ok,
            f"Position {pct:.1%} vs limit {self.limits.max_position_pct:.1%}",
            limits={"max_position_pct": self.limits.max_position_pct, "position_pct": pct},
        )
        self._emit(
            "position_limit",
            result,
            {"position_value": position_value, "total_equity": total_equity},
        )
        return result

    def check_leverage(self, total_exposure: float, total_equity: float) -> RiskCheckResult:
        lev = total_exposure / max(total_equity, 1.0)
        ok = lev <= self.limits.max_leverage
        result = RiskCheckResult(
            ok,
            f"Leverage {lev:.1f}x vs limit {self.limits.max_leverage:.1f}x",
            limits={"max_leverage": self.limits.max_leverage, "leverage": lev},
        )
        self._emit(
            "leverage", result, {"total_exposure": total_exposure, "total_equity": total_equity}
        )
        return result

    def check_daily_loss(self, daily_pnl: float, total_equity: float) -> RiskCheckResult:
        pct = abs(min(daily_pnl, 0)) / max(total_equity, 1.0)
        ok = pct < self.limits.max_daily_loss_pct
        result = RiskCheckResult(
            ok,
            f"Daily loss {pct:.2%} vs limit {self.limits.max_daily_loss_pct:.2%}",
            limits={"max_daily_loss_pct": self.limits.max_daily_loss_pct, "daily_loss_pct": pct},
        )
        self._emit("daily_loss", result, {"daily_pnl": daily_pnl, "total_equity": total_equity})
        if pct >= self.limits.daily_cb_force:
            self._escalate(SystemState.EMERGENCY, result.reason)
        elif not ok:
            self._escalate(SystemState.SAFE_MODE, result.reason)
        return result

    def check_var(self, returns: np.ndarray) -> RiskCheckResult:
        if len(returns) < 10:
            result = RiskCheckResult(
                True, "Insufficient data", limits={"max_var_pct": self.limits.max_var_pct}
            )
            self._emit("var", result, {"sample_size": len(returns)})
            return result
        var = abs(np.percentile(returns, (1 - self.limits.var_confidence) * 100))
        ok = var <= self.limits.max_var_pct
        result = RiskCheckResult(
            ok,
            f"VaR {var:.2%} vs limit {self.limits.max_var_pct:.2%}",
            limits={"max_var_pct": self.limits.max_var_pct, "var_pct": float(var)},
        )
        self._emit("var", result, {"sample_size": len(returns)})
        if not ok:
            self._escalate(SystemState.SAFE_MODE, result.reason)
        return result

    def check_mdd(self, current_mdd: float) -> RiskCheckResult:
        if current_mdd >= self.limits.mdd_liquidate:
            result = RiskCheckResult(
                False,
                f"MDD {current_mdd:.1%} - LIQUIDATE ALL",
                limits={
                    "mdd_reduce": self.limits.mdd_reduce,
                    "mdd_liquidate": self.limits.mdd_liquidate,
                },
            )
            self._emit("mdd", result, {"current_mdd": current_mdd})
            self._escalate(SystemState.EMERGENCY, result.reason)
            return result
        if current_mdd >= self.limits.mdd_reduce:
            result = RiskCheckResult(
                False,
                f"MDD {current_mdd:.1%} - REDUCE 50%",
                limits={
                    "mdd_reduce": self.limits.mdd_reduce,
                    "mdd_liquidate": self.limits.mdd_liquidate,
                },
            )
            self._emit("mdd", result, {"current_mdd": current_mdd})
            self._escalate(SystemState.SAFE_MODE, result.reason)
            return result
        result = RiskCheckResult(
            True,
            "MDD OK",
            limits={
                "mdd_reduce": self.limits.mdd_reduce,
                "mdd_liquidate": self.limits.mdd_liquidate,
            },
        )
        self._emit("mdd", result, {"current_mdd": current_mdd})
        return result
