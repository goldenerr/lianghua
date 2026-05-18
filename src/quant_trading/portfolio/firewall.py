
"""Global capital firewall & dynamic allocation (portfolio-003).
AGENTS.md: 策略间资金防火墙, 基于组合VaR的动态调整."""
import numpy as np
class CapitalFirewall:
    def __init__(self, total_capital: float, max_per_strategy: float = 0.30):
        self.total = total_capital; self.max_pct = max_per_strategy; self.allocations = {}
    def allocate(self, strategy_id: str, amount: float) -> bool:
        if amount / self.total > self.max_pct: return False
        if sum(self.allocations.values()) + amount > self.total: return False
        self.allocations[strategy_id] = amount; return True
    def release(self, strategy_id: str) -> float:
        return self.allocations.pop(strategy_id, 0.0)
    def available(self) -> float: return self.total - sum(self.allocations.values())
    def adjust_for_var(self, var_limits: dict[str, float]) -> dict:
        adjustments = {}
        for sid, var_limit in var_limits.items():
            if sid in self.allocations:
                max_allowed = var_limit * self.total
                if self.allocations[sid] > max_allowed:
                    adjustments[sid] = self.allocations[sid] - max_allowed
                    self.allocations[sid] = max_allowed
        return adjustments
