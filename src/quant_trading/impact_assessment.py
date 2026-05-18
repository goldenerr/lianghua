
"""Capital impact assessment template (docs-002). AGENTS.md: standardized impact report generator."""
from dataclasses import dataclass
from datetime import datetime, timezone
@dataclass
class ImpactAssessment:
    change_id: str; max_potential_loss: float; liquidity_risk: str="low"; extreme_case_loss: float=0.0
    approved: bool=False; assessed_at: str=""
    def __post_init__(self): self.assessed_at = datetime.now(timezone.utc).isoformat()
    def to_report(self) -> dict: return {"max_loss": self.max_potential_loss, "approved": self.approved}
