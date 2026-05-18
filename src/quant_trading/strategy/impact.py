
"""Parameter change impact assessment (strategy-003). AGENTS.md: auto short-term WF comparison."""
from dataclasses import dataclass
@dataclass
class ChangeReport:
    sharpe_before: float; sharpe_after: float; approved: bool = False
