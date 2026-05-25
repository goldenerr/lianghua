"""
Dynamic factor weights via rolling IC (Information Coefficient).

Every rebalance: compute rank IC for each factor over the past N months
on the current universe. Use IC magnitude as weight (higher |IC| = more weight).
Weights are EMA-smoothed to reduce noise.
"""
from __future__ import annotations

import numpy as np


def rolling_spearman_ic(
    factor_values: dict[str, np.ndarray],  # {factor_name: [N_stocks]}
    forward_returns: np.ndarray,           # [N_stocks] next-period returns
    factors: list[str] | None = None,
) -> dict[str, float]:
    """Compute rank IC for each factor against forward returns.
    
    IC = Spearman rank correlation between factor rank and future return rank.
    """
    if factors is None:
        factors = list(factor_values.keys())

    ic = {}
    for name in factors:
        fv = factor_values.get(name)
        if fv is None or len(fv) < 10:
            ic[name] = 0.0
            continue

        # Remove NaN
        mask = ~np.isnan(fv) & ~np.isnan(forward_returns)
        if mask.sum() < 10:
            ic[name] = 0.0
            continue

        f_clean = fv[mask]
        r_clean = forward_returns[mask]

        # Spearman = Pearson on ranks
        f_rank = np.argsort(np.argsort(f_clean)).astype(float)
        r_rank = np.argsort(np.argsort(r_clean)).astype(float)

        f_centered = f_rank - f_rank.mean()
        r_centered = r_rank - r_rank.mean()

        denom = np.sqrt((f_centered ** 2).sum() * (r_centered ** 2).sum())
        if denom < 1e-12:
            ic[name] = 0.0
        else:
            ic[name] = float((f_centered * r_centered).sum() / denom)

    return ic


class AdaptiveWeights:
    """EMA-smoothed factor weights based on rolling IC.
    
    For each rebalance:
      1. Compute factor values and forward returns for all stocks
      2. Compute rank IC per factor
      3. Update EMA of |IC| per factor
      4. Normalize to sum=1
    """

    def __init__(
        self,
        base_weights: dict[str, float],
        factors: list[str] | None = None,
        ic_halflife: float = 6.0,   # EMA halflife in rebalance periods (~6 quarters)
        min_weight: float = 0.02,   # floor to prevent factor extinction
        max_weight: float = 0.40,   # cap to prevent single-factor domination
    ):
        self.base_weights = dict(base_weights)
        self.factors = factors or list(base_weights.keys())
        self.ic_halflife = ic_halflife
        self.alpha = 1.0 - np.exp(np.log(0.5) / ic_halflife)  # EMA decay
        self.min_weight = min_weight
        self.max_weight = max_weight

        # EMA state
        self._ema_abs_ic: dict[str, float] = {f: np.nan for f in self.factors}

    def update(
        self,
        factor_values: dict[str, np.ndarray],
        forward_returns: np.ndarray,
    ) -> dict[str, float]:
        """Update EMA weights from new IC observations.
        
        Returns updated weights dict.
        """
        ic = rolling_spearman_ic(factor_values, forward_returns, self.factors)

        # Update EMA of |IC|
        for name in self.factors:
            abs_ic = abs(ic.get(name, 0.0))
            if np.isnan(self._ema_abs_ic[name]):
                self._ema_abs_ic[name] = abs_ic
            else:
                self._ema_abs_ic[name] = (
                    self.alpha * abs_ic + (1 - self.alpha) * self._ema_abs_ic[name]
                )

        # Normalize to weights (IC-driven with floor)
        weights = {}
        for name in self.factors:
            w = self._ema_abs_ic[name]
            # Blend with base weight (50/50) to prevent extreme drift
            base_w = self.base_weights.get(name, 0.0)
            w = 0.5 * w + 0.5 * base_w
            weights[name] = max(self.min_weight, min(self.max_weight, w))

        # Normalize
        total = sum(weights.values())
        if total > 0:
            weights = {k: v / total for k, v in weights.items()}

        return weights

    def current_weights(self) -> dict[str, float]:
        """Get current weights based on EMA state (without update)."""
        weights = {}
        for name in self.factors:
            w = self._ema_abs_ic.get(name, np.nan)
            if np.isnan(w):
                w = self.base_weights.get(name, 0.0)
            base_w = self.base_weights.get(name, 0.0)
            w = 0.5 * w + 0.5 * base_w
            weights[name] = max(self.min_weight, min(self.max_weight, w))

        total = sum(weights.values())
        if total > 0:
            weights = {k: v / total for k, v in weights.items()}
        return weights


# ── Quick test ────────────────────────────────────────────────
if __name__ == "__main__":
    rng = np.random.RandomState(42)

    # Simulate: 100 stocks, 3 factors, 10 periods
    N = 100

    aw = AdaptiveWeights(
        base_weights={"rsi": 0.25, "momentum": 0.25, "vol": 0.50},
        ic_halflife=3.0,
    )

    for period in range(10):
        fv = {
            "rsi": rng.normal(0, 1, N),
            "momentum": rng.normal(0.2, 1, N),
            "vol": rng.normal(-0.1, 1, N),
        }
        # Returns driven more by momentum (true IC higher)
        fwd_returns = 0.3 * fv["momentum"] + 0.1 * fv["rsi"] + 0.05 * fv["vol"] + rng.normal(0, 0.5, N)

        weights = aw.update(fv, fwd_returns)
        print(f"  Period {period}: weights = {', '.join(f'{k}={v:.3f}' for k, v in weights.items())}")

    print(f"\n  Final: {aw.current_weights()}")
    print("  Expected: momentum > rsi > vol (momentum has true IC=0.3)")
