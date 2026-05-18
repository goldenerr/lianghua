
"""Architecture & risk policy documentation generator (docs-001).
AGENTS.md §34: C4图, 序列图, NFR, 指标库, 版本化, 变更影响矩阵."""
from dataclasses import dataclass, field
@dataclass
class ArchitectureDoc:
    title: str = "Quant Trading System"; version: str = "2.13.0"
    nfr: dict = field(default_factory=lambda: {"latency": "≤10ms market→signal, ≤60ms signal→order",
        "availability": "99.9%", "rto": "≤2h", "rpo": "≤1h", "capacity": "200 symbols, 20 strategies"})
    c4_context: str = "Investors → QuantSystem → Exchanges/MarketData/Banks"
    def render(self) -> str: return f"# {self.title} v{self.version}\n\n## NFR\n{self.nfr}\n\n## C4 Context\n{self.c4_context}"
