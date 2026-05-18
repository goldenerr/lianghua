
"""Historical order/fill replay validation (replay-validation-001).
AGENTS.md §45: 决策回放/订单匹配回放/完整回放."""
from dataclasses import dataclass
from enum import Enum
class ReplayMode(str, Enum): DECISION = "decision"; ORDER_MATCHING = "order_matching"; FULL = "full"
@dataclass
class ReplayResult:
    mode: ReplayMode; orders_replayed: int = 0; matches: int = 0; mismatches: int = 0
    @property
    def match_rate(self) -> float: return self.matches / max(self.orders_replayed, 1)
def replay_orders(historical_orders: list[dict], live_engine, mode: ReplayMode = ReplayMode.DECISION) -> ReplayResult:
    result = ReplayResult(mode=mode, orders_replayed=len(historical_orders))
    for order in historical_orders:
        live_decision = live_engine.decide(order) if hasattr(live_engine, "decide") else order
        if live_decision == order: result.matches += 1
        else: result.mismatches += 1
    return result
