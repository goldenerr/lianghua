"""
Multi-layered risk management system (risk-001).

AGENTS.md §6: Pre-trade, in-trade, post-trade risk checks.
"""
from dataclasses import dataclass, field
from typing import Optional
import numpy as np

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
    def __init__(self, limits: Optional[RiskLimits] = None):
        self.limits = limits or RiskLimits()
        self._daily_pnl: float = 0.0
        self._consecutive_losses: int = 0

    def check_position_limit(self, position_value: float, total_equity: float) -> RiskCheckResult:
        pct = abs(position_value) / max(total_equity, 1.0)
        ok = pct <= self.limits.max_position_pct
        return RiskCheckResult(ok, f"Position {pct:.1%} vs limit {self.limits.max_position_pct:.1%}")

    def check_leverage(self, total_exposure: float, total_equity: float) -> RiskCheckResult:
        lev = total_exposure / max(total_equity, 1.0)
        ok = lev <= self.limits.max_leverage
        return RiskCheckResult(ok, f"Leverage {lev:.1f}x vs limit {self.limits.max_leverage:.1f}x")

    def check_daily_loss(self, daily_pnl: float, total_equity: float) -> RiskCheckResult:
        pct = abs(min(daily_pnl, 0)) / max(total_equity, 1.0)
        ok = pct < self.limits.max_daily_loss_pct
        return RiskCheckResult(ok, f"Daily loss {pct:.2%} vs limit {self.limits.max_daily_loss_pct:.2%}")

    def check_var(self, returns: np.ndarray) -> RiskCheckResult:
        if len(returns) < 10: return RiskCheckResult(True, "Insufficient data")
        var = abs(np.percentile(returns, (1 - self.limits.var_confidence) * 100))
        ok = var <= self.limits.max_var_pct
        return RiskCheckResult(ok, f"VaR {var:.2%} vs limit {self.limits.max_var_pct:.2%}")

    def check_mdd(self, current_mdd: float) -> RiskCheckResult:
        if current_mdd >= self.limits.mdd_liquidate:
            return RiskCheckResult(False, f"MDD {current_mdd:.1%} - LIQUIDATE ALL")
        if current_mdd >= self.limits.mdd_reduce:
            return RiskCheckResult(False, f"MDD {current_mdd:.1%} - REDUCE 50%")
        return RiskCheckResult(True, "MDD OK")
