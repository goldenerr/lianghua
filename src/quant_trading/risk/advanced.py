"""
Advanced risk modules (risk-002 through risk-006).
AGENTS.md: Greeks, cross-market margin, dynamic stress, model risk, kill switch.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import erf, exp, log, pi, sqrt

UTC = timezone.utc

# risk-002: Option Greeks
class GreeksCalculator:
    @staticmethod
    def _norm_cdf(x: float) -> float:
        return 0.5 * (1.0 + erf(x / sqrt(2.0)))

    @staticmethod
    def _norm_pdf(x: float) -> float:
        return exp(-0.5 * x * x) / sqrt(2.0 * pi)

    @staticmethod
    def delta(S: float, K: float, T: float, r: float, sigma: float, is_call: bool = True) -> dict:
        """
        Black-Scholes Greeks computation.

        Returns: delta, gamma, vega, theta
        """
        if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
            return {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}

        vol_sqrt_t = sigma * sqrt(T)
        d1 = (log(S / K) + (r + 0.5 * sigma * sigma) * T) / vol_sqrt_t
        d2 = d1 - vol_sqrt_t

        nd1 = GreeksCalculator._norm_cdf(d1)
        nd2 = GreeksCalculator._norm_cdf(d2)
        pdf_d1 = GreeksCalculator._norm_pdf(d1)

        if is_call:
            delta = nd1
            theta = -(S * pdf_d1 * sigma) / (2 * sqrt(T)) - r * K * exp(-r * T) * nd2
        else:
            delta = nd1 - 1.0
            theta = -(S * pdf_d1 * sigma) / (2 * sqrt(T)) + r * K * exp(-r * T) * GreeksCalculator._norm_cdf(-d2)

        gamma = pdf_d1 / (S * vol_sqrt_t)
        vega = S * pdf_d1 * sqrt(T)
        return {"delta": float(delta), "gamma": float(gamma), "vega": float(vega), "theta": float(theta)}

# risk-003: Cross-market margin
class CrossMarketMargin:
    def __init__(self): self._accounts: dict = {}
    def add_account(self, name: str, equity: float, margin_used: float):
        if equity < 0 or margin_used < 0:
            raise ValueError("equity and margin_used must be non-negative")
        if margin_used > equity:
            raise ValueError("margin_used cannot exceed equity")
        self._accounts[name] = {"equity": equity, "margin": margin_used}

    def total_exposure(self) -> float: return sum(a["margin"] for a in self._accounts.values())
    def available_margin(self) -> float: return sum(a["equity"] - a["margin"] for a in self._accounts.values())

# risk-006: Kill Switch
class KillSwitch:
    def __init__(self):
        self._active = False
        self.reason: str = ""
        self.activated_at: str | None = None
        self.last_transition_at: str | None = None

    def activate(self, reason: str) -> None:
        self._active = True
        self.reason = reason
        now = datetime.now(UTC).isoformat()
        self.activated_at = self.activated_at or now
        self.last_transition_at = now

    def deactivate(self) -> None:
        self._active = False
        self.reason = ""
        self.activated_at = None
        self.last_transition_at = datetime.now(UTC).isoformat()

    @property
    def is_active(self) -> bool: return self._active
