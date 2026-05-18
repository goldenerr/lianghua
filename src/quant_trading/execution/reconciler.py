
"""Position reconciler (exec-002). AGENTS.md §18: 30s reconciliation, Safe Mode on mismatch."""
class PositionReconciler:
    def __init__(self, tolerance: float = 0.0001): self.tolerance = tolerance
    def reconcile(self, internal: float, exchange: float) -> bool:
        diff = abs(internal - exchange) / max(abs(exchange), 1.0)
        return diff <= self.tolerance
