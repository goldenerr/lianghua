
"""Benchmark comparison & Brinson attribution (portfolio-002).
AGENTS.md §53: 风格/行业/因子/时机/选股归因."""
import numpy as np
def brinson_attribution(portfolio_weights, benchmark_weights, sector_returns, benchmark_sector_returns):
    allocation = sum((w - b) * br for w, b, br in zip(portfolio_weights, benchmark_weights, benchmark_sector_returns))
    selection = sum(b * (sr - br) for b, sr, br in zip(benchmark_weights, sector_returns, benchmark_sector_returns))
    interaction = sum((w - b) * (sr - br) for w, b, sr, br in zip(portfolio_weights, benchmark_weights, sector_returns, benchmark_sector_returns))
    return {"allocation_effect": round(allocation, 6), "selection_effect": round(selection, 6),
            "interaction_effect": round(interaction, 6), "total_excess": round(allocation + selection + interaction, 6)}
