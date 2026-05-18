
"""Performance attribution (portfolio-002). AGENTS.md §53: Brinson model."""
def brinson_attribute(portfolio_weights: list, benchmark_weights: list, returns: list) -> dict:
    alloc = sum((w - b) * r for w, b, r in zip(portfolio_weights, benchmark_weights, [sum(returns)/len(returns)]*len(returns)))
    select = sum(b * (r - sum(returns)/len(returns)) for b, r in zip(benchmark_weights, returns))
    return {"allocation": alloc, "selection": select, "total": alloc + select}
