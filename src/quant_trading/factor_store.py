
"""Feature/factor unified management (factor-001). AGENTS.md: Feast-based, optional for large teams."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
@dataclass
class FactorDefinition:
    name: str; entity: str="symbol"; dtype: str="float64"; refresh_interval: str="daily"
@dataclass
class FactorStore:
    factors: dict = field(default_factory=dict)
    def register(self, factor: FactorDefinition) -> None: self.factors[factor.name] = factor
    def get(self, name: str) -> FactorDefinition: return self.factors.get(name)
