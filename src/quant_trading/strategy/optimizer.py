
"""Strategy parameter optimization (strategy-002). AGENTS.md: Optuna/GridSearch."""
from itertools import product
def grid_search(param_grid: dict) -> list[dict]:
    keys, vals = param_grid.keys(), param_grid.values()
    return [dict(zip(keys, combo)) for combo in product(*vals)]
