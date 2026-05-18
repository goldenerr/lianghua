"""
High-frequency low-latency path (exec-004, optional).
AGENTS.md §13: 行情→信号≤10ms, 信号→订单≤60ms. C++/Rust hot path placeholder.
"""
from dataclasses import dataclass
from time import perf_counter_ns

@dataclass
class LatencyBudget: market_data_ns: int = 10_000_000; signal_ns: int = 30_000_000; order_ns: int = 60_000_000

class FastPath:
    """Fast execution path wrapper. Real implementation in Rust/C++ via FFI."""
    def __init__(self, budget: LatencyBudget = None):
        self.budget = budget or LatencyBudget(); self._t0 = 0
    def start_tick(self) -> None: self._t0 = perf_counter_ns()
    def elapsed_us(self) -> float: return (perf_counter_ns() - self._t0) / 1000.0
    def check_latency(self, stage: str) -> bool:
        elapsed = self.elapsed_us(); limits = {"market_data": 10000, "signal": 30000, "order": 60000}
        return elapsed < limits.get(stage, 60000)
