
"""Strategy lifecycle manager (portfolio-004). AGENTS.md: full lifecycle from creation to retirement."""
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
class Stage(str, Enum): INCUBATION="incubation"; PAPER="paper"; SMALL_LIVE="small_live"; FULL_LIVE="full_live"; RETIRED="retired"
@dataclass
class StrategyLifecycle:
    strategy_id: str; stage: Stage=Stage.INCUBATION; created: str=""
    def __post_init__(self): self.created = self.created or datetime.now(timezone.utc).isoformat()
    def promote(self) -> bool:
        order = list(Stage); idx = order.index(self.stage)
        if idx < len(order)-1: self.stage = order[idx+1]; return True
        return False
    def retire(self) -> None: self.stage = Stage.RETIRED
