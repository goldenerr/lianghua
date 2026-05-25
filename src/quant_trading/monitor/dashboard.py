"""Live strategy performance dashboard (monitor-002). AGENTS.md: mobile intervention, secondary confirmation."""

from dataclasses import dataclass, field


@dataclass
class StrategyDashboard:
    strategy_id: str
    sharpe_20d: float = 0.0
    pnl_today: float = 0.0
    health_score: float = 100.0
    active: bool = True
    alerts: list = field(default_factory=list)

    def intervene(self, action: str, reason: str = "") -> dict:
        return {"action": action, "reason": reason, "confirmed": False, "requires_2fa": True}

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy_id,
            "sharpe": self.sharpe_20d,
            "pnl": self.pnl_today,
            "health": self.health_score,
            "active": self.active,
        }
