
"""Regulatory compliance reporting + tax calculation (compliance-001).
AGENTS.md §39: 监管模板, 多监管区切换, PDF/XML, WORM存档."""
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
class Jurisdiction(str, Enum): CN = "cn"; US = "us"; HK = "hk"; EU = "eu"
@dataclass
class TaxCalculator:
    jurisdiction: Jurisdiction = Jurisdiction.CN
    def stamp_duty(self, notional: float) -> float:
        rates = {Jurisdiction.CN: 0.0005, Jurisdiction.HK: 0.001, Jurisdiction.US: 0.0, Jurisdiction.EU: 0.0}
        return round(notional * rates.get(self.jurisdiction, 0), 2)
    def capital_gains(self, profit: float) -> float:
        rates = {Jurisdiction.CN: 0.0, Jurisdiction.HK: 0.0, Jurisdiction.US: 0.20, Jurisdiction.EU: 0.25}
        return round(max(0, profit) * rates.get(self.jurisdiction, 0), 2)
@dataclass
class ComplianceReport:
    report_id: str; jurisdiction: Jurisdiction; period_start: date; period_end: date
    total_trades: int = 0; total_volume: float = 0.0; stamp_duty_paid: float = 0.0
    capital_gains_tax: float = 0.0; generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    def to_worm(self) -> bytes: return str(self.__dict__).encode()
