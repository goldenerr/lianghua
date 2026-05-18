
"""Regulatory compliance reporting (compliance-001). AGENTS.md §39: PDF/XML templates."""
from dataclasses import dataclass
@dataclass
class ComplianceReport:
    report_type: str; jurisdiction: str; data: dict
    def to_dict(self) -> dict: return self.data
