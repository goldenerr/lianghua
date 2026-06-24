"""
V5.9 Paper Trading Engine — AGENTS.md §7 & §9 Gate 2.
Simulates live trading with real market data, dynamic slippage, and MDD safeguards.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from quant_trading.execution.algorithms import resolve_execution_fee_model_by_id

log = logging.getLogger("paper_trading")
_A_SHARE_CODE_RE = re.compile(r"(\d{6})")

# ── V5.9 Production Config ──────────────────────────────────
V59_CONFIG = {
    "top_n": 35,
    "rebalance_freq": 90,
    "max_position_pct": 0.20,
    "max_per_sector": 5,
    "slippage_base": 0.0005,
    "slippage_factor": 0.10,
    "fee_model": "cn_stock",
    "risk_free_rate": 0.025,
    "warmup_days": 252,
    "min_history": 252,
    "min_stocks": 50,
    # MDD safeguards (V5.9 tightened)
    "mdd_reduce_threshold": 0.10,
    "mdd_reduce_scale": 0.50,
    "mdd_stop_threshold": 0.18,
    "mdd_stop_scale": 0.25,
}

V35_WEIGHTS = {
    "rsi": 0.25,
    "bollinger": 0.25,
    "momentum": 0.20,
    "macd": 0.15,
    "vol_dev": 0.10,
    "low_vol": 0.05,
}


# ── Factor Functions (V3.5 exact) ─────────────────────────
def factor_rsi_mr(closes: np.ndarray) -> float:
    if len(closes) < 15:
        return np.nan
    d = np.diff(closes[-15:])
    g = np.clip(d, 0, None).mean()
    loss_avg = -np.clip(d, None, 0).mean()
    if loss_avg < 1e-12:
        return np.nan
    return float(abs(100.0 - 100.0 / (1.0 + g / loss_avg) - 50.0))


def factor_bollinger_mr(closes: np.ndarray) -> float:
    if len(closes) < 20:
        return np.nan
    m = closes[-20:].mean()
    s = closes[-20:].std(ddof=1)
    w = 4.0 * s
    return float(abs(closes[-1] - m) / w) if w > 1e-12 else 0.0


def factor_momentum(closes: np.ndarray) -> float:
    if len(closes) < 68:
        return np.nan
    return float(closes[-1] / closes[-68] - 1)


def _ema(s: np.ndarray, span: int) -> float:
    if len(s) < span:
        return np.nan
    a = 2.0 / (span + 1)
    r = s[:span].mean()
    for i in range(span, len(s)):
        r = a * s[i] + (1 - a) * r
    return float(r)


def factor_macd(closes: np.ndarray) -> float:
    if len(closes) < 35:
        return np.nan
    return _ema(closes, 12) - _ema(closes, 26)


def factor_vol_dev(volumes: np.ndarray) -> float:
    if len(volumes) < 20:
        return np.nan
    m = volumes[-20:].mean()
    return float(-abs(volumes[-1] / m - 1.0)) if m > 1e-12 else 0.0


def factor_low_vol(closes: np.ndarray) -> float:
    if len(closes) < 61:
        return np.nan
    r = np.diff(closes[-61:]) / closes[-61:-1]
    return float(-np.std(r, ddof=1) * np.sqrt(252))


MR: dict[str, tuple[Callable[[np.ndarray], float], str]] = {
    "rsi": (factor_rsi_mr, "close"),
    "bollinger": (factor_bollinger_mr, "close"),
    "momentum": (factor_momentum, "close"),
    "macd": (factor_macd, "close"),
    "vol_dev": (factor_vol_dev, "volume"),
    "low_vol": (factor_low_vol, "close"),
}


@dataclass
class PaperPosition:
    symbol: str
    shares: int = 0
    avg_cost: float = 0.0
    market_value: float = 0.0


@dataclass
class PaperOrder:
    symbol: str
    side: str  # "buy" or "sell"
    quantity: int
    order_type: str = "market"
    limit_price: float | None = None
    status: str = "pending"
    filled_qty: int = 0
    filled_price: float = 0.0
    created_at: str = ""
    client_id: str = ""

    def __post_init__(self) -> None:
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.client_id = f"paper_{self.symbol}_{int(time.time()*1e6)}"


@dataclass
class PaperAccount:
    cash: float
    initial_capital: float
    positions: dict[str, PaperPosition] = field(default_factory=dict)
    equity_history: list[float] = field(default_factory=list)
    trade_log: list[dict[str, Any]] = field(default_factory=list)
    peak_equity: float = 0.0

    def __post_init__(self) -> None:
        self.peak_equity = self.cash
        self.equity_history.append(self.cash)

    @property
    def total_equity(self) -> float:
        mv = sum(p.market_value for p in self.positions.values())
        return self.cash + mv

    @property
    def current_drawdown(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return (self.total_equity - self.peak_equity) / self.peak_equity

    def update_market_values(self, prices: dict[str, float]) -> None:
        for sym, pos in self.positions.items():
            if sym in prices:
                pos.market_value = pos.shares * prices[sym]
        self.peak_equity = max(self.peak_equity, self.total_equity)
        self.equity_history.append(self.total_equity)


class PaperTradingEngine:
    """Paper trading engine for V5.9 strategy validation (AGENTS.md §9 Gate 2)."""

    def __init__(
        self,
        initial_capital: float = 1_000_000,
        config: dict[str, float | int | str] | None = None,
        industry_data_path: str | Path | None = None,
    ) -> None:
        self.config = config or V59_CONFIG
        self.account = PaperAccount(cash=initial_capital, initial_capital=initial_capital)
        self.orders: list[PaperOrder] = []
        self.days_traded = 0
        self.last_rebalance_day = -999
        self.stopped = False
        self.industry_map: dict[str, str] = {}
        self.industry_data_path = (
            Path(industry_data_path) if industry_data_path else Path("data/industry_fixed.parquet")
        )
        self._load_industries()

    def _fee_rate(self, side: str) -> float:
        """Return the configured execution fee rate for paper-trading fills."""
        fee_model_id = str(self.config.get("fee_model", "cn_stock"))
        return resolve_execution_fee_model_by_id(fee_model_id).fee_rate(side, "taker")

    def _cfg_float(self, key: str, default: float) -> float:
        value = self.config.get(key, default)
        if not isinstance(value, int | float):
            raise ValueError(f"{key} must be numeric")
        return float(value)

    def _cfg_int(self, key: str, default: int) -> int:
        value = self.config.get(key, default)
        if not isinstance(value, int | float):
            raise ValueError(f"{key} must be numeric")
        return int(value)

    def _load_industries(self) -> None:
        """Load industry classification for sector caps."""
        try:
            if not self.industry_data_path.exists():
                return
            df = pd.read_parquet(self.industry_data_path)
            for _, row in df.iterrows():
                code_match = _A_SHARE_CODE_RE.search(str(row["code"]))
                ind = row.get("industry", "")
                if code_match and ind and not pd.isna(ind):
                    self.industry_map[code_match.group(1)] = str(ind)
        except Exception as e:
            log.warning(f"Industry data load failed: {e}")

    def check_mdd_safeguards(self) -> str:
        """Check and apply MDD safeguards. Returns action taken."""
        dd = self.account.current_drawdown
        stop_threshold = self._cfg_float("mdd_stop_threshold", 0.18)
        reduce_threshold = self._cfg_float("mdd_reduce_threshold", 0.10)
        stop_scale = self._cfg_float("mdd_stop_scale", 0.25)

        if dd < -stop_threshold:
            return (
                f"FLOOR: DD={dd:.1%} exceeds stop threshold {stop_threshold:.0%}; "
                f"keep {stop_scale:.0%} recovery exposure"
            )
        elif dd < -reduce_threshold:
            return f"REDUCED: DD={dd:.1%} exceeds reduce threshold {reduce_threshold:.0%}"
        return "OK"

    def compute_positions(
        self, snapshot: dict[str, dict[str, Any]], current_date: object
    ) -> dict[str, float]:
        """Compute target weights using V5.9 factor model."""
        return self._compute_positions_inline(snapshot)

    def _compute_positions_inline(self, snapshot: dict[str, dict[str, Any]]) -> dict[str, float]:
        """Inline factor computation (avoids import issues)."""
        weights = V35_WEIGHTS
        factor_names = list(weights.keys())

        raw: dict[str, dict[str, float]] = {}
        for sym, data in snapshot.items():
            fv: dict[str, float] = {}
            for name in factor_names:
                fn, dt = MR[name]
                arr = np.asarray(data["close"] if dt == "close" else data.get("volume", []))
                fv[name] = fn(arr)
            if not all(np.isnan(v) for v in fv.values()):
                raw[sym] = fv

        if not raw:
            return {}

        # Cross-sectional z-score
        cz: dict[str, tuple[float, float]] = {}
        for name in factor_names:
            vals = [fv.get(name, np.nan) for fv in raw.values()]
            valid = [v for v in vals if not np.isnan(v)]
            if len(valid) >= 5:
                cz[name] = (float(np.mean(valid)), float(np.std(valid, ddof=1)))

        # Score and rank
        from quant_trading.strategy.factors import composite_score

        ranked: list[tuple[str, float]] = []
        for sym, fv in raw.items():
            cs = composite_score(fv, weights, cz)
            if not np.isnan(cs):
                ranked.append((sym, cs))
        ranked.sort(key=lambda x: x[1], reverse=True)

        # Apply sector caps
        top_n = self._cfg_int("top_n", 35)
        max_sec = self._cfg_int("max_per_sector", 5)
        selected: list[str] = []
        sec_counts: dict[str, int] = {}
        for sym, _score in ranked:
            ind = self.industry_map.get(sym, "__UNKNOWN__")
            if sec_counts.get(ind, 0) < max_sec:
                selected.append(sym)
                sec_counts[ind] = sec_counts.get(ind, 0) + 1
            if len(selected) >= top_n:
                break

        if not selected:
            return {}

        n = len(selected)
        w = min(1.0 / n, self._cfg_float("max_position_pct", 0.20))
        if w * n > 1.0:
            w = 1.0 / n

        # Apply MDD safeguards
        dd = self.account.current_drawdown
        if dd < -self._cfg_float("mdd_stop_threshold", 0.18):
            w *= self._cfg_float("mdd_stop_scale", 0.25)
        elif dd < -self._cfg_float("mdd_reduce_threshold", 0.10):
            w *= self._cfg_float("mdd_reduce_scale", 0.50)

        return {s: w for s in selected}

    def execute_rebalance(
        self, target_weights: dict[str, float], prices: dict[str, float], date_str: str
    ) -> None:
        """Execute orders to reach target weights. Simulates slippage + costs."""
        # Calculate current weights
        total_eq = self.account.total_equity
        current_weights: dict[str, float] = {}
        for sym, pos in self.account.positions.items():
            if pos.market_value > 0:
                current_weights[sym] = pos.market_value / total_eq

        # Generate orders
        for sym, tw in target_weights.items():
            cw = current_weights.get(sym, 0.0)
            diff = tw - cw

            if abs(diff) < 0.001:  # Skip tiny adjustments
                continue

            price = prices.get(sym, 0)
            if price <= 0:
                continue

            target_value = total_eq * tw
            current_value = cw * total_eq

            if diff > 0:  # Buy
                slippage = self._cfg_float("slippage_base", 0.0005) * (
                    1 + self._cfg_float("slippage_factor", 0.10) * abs(diff)
                )
                exec_price = price * (1 + slippage)
                cost = target_value - current_value
                buy_fee_rate = self._fee_rate("buy")
                cost_with_fees = cost * (1 + buy_fee_rate)

                if cost_with_fees <= self.account.cash:
                    shares = int(target_value / exec_price / 100) * 100  # Round to lots
                    if shares > 0:
                        actual_cost = shares * exec_price * (1 + buy_fee_rate)
                        if actual_cost <= self.account.cash:
                            self.account.cash -= actual_cost
                            if sym not in self.account.positions:
                                self.account.positions[sym] = PaperPosition(symbol=sym)
                            pos = self.account.positions[sym]
                            pos.shares += shares
                            pos.avg_cost = (
                                ((pos.avg_cost * (pos.shares - shares)) + actual_cost) / pos.shares
                                if pos.shares > 0
                                else exec_price
                            )

                            self.account.trade_log.append(
                                {
                                    "date": date_str,
                                    "symbol": sym,
                                    "side": "buy",
                                    "shares": shares,
                                    "price": round(exec_price, 2),
                                    "cost": round(actual_cost, 2),
                                    "slippage": round(slippage, 4),
                                }
                            )

            elif diff < 0:  # Sell
                sell_position = self.account.positions.get(sym)
                if not sell_position or sell_position.shares <= 0:
                    continue

                slippage = self._cfg_float("slippage_base", 0.0005) * (
                    1 + self._cfg_float("slippage_factor", 0.10) * abs(diff)
                )
                exec_price = price * (1 - slippage)
                sell_value = abs(current_value - target_value)
                shares_to_sell = min(sell_position.shares, int(sell_value / exec_price / 100) * 100)

                if shares_to_sell > 0:
                    proceeds = shares_to_sell * exec_price * (1 - self._fee_rate("sell"))
                    self.account.cash += proceeds
                    sell_position.shares -= shares_to_sell
                    if sell_position.shares <= 0:
                        del self.account.positions[sym]

                    self.account.trade_log.append(
                        {
                            "date": date_str,
                            "symbol": sym,
                            "side": "sell",
                            "shares": shares_to_sell,
                            "price": round(exec_price, 2),
                            "proceeds": round(proceeds, 2),
                            "slippage": round(slippage, 4),
                        }
                    )

        # Sell positions not in targets
        for sym in list(self.account.positions.keys()):
            if sym not in target_weights:
                pos = self.account.positions[sym]
                if pos.shares <= 0:
                    continue
                price = prices.get(sym, 0)
                if price <= 0:
                    continue
                exec_price = price * (1 - self._cfg_float("slippage_base", 0.0005))
                proceeds = pos.shares * exec_price * (1 - self._fee_rate("sell"))
                self.account.cash += proceeds
                self.account.trade_log.append(
                    {
                        "date": date_str,
                        "symbol": sym,
                        "side": "sell",
                        "shares": pos.shares,
                        "price": round(exec_price, 2),
                        "proceeds": round(proceeds, 2),
                        "reason": "removed from portfolio",
                    }
                )
                del self.account.positions[sym]

    def get_status(self) -> dict[str, Any]:
        """Get current paper trading status."""
        return {
            "date": datetime.now(timezone.utc).isoformat(),
            "days_traded": self.days_traded,
            "stopped": self.stopped,
            "cash": round(self.account.cash, 2),
            "total_equity": round(self.account.total_equity, 2),
            "total_return": round(self.account.total_equity / self.account.initial_capital - 1, 4),
            "drawdown": round(self.account.current_drawdown, 4),
            "positions": len(self.account.positions),
            "total_trades": len(self.account.trade_log),
            "mdd_status": self.check_mdd_safeguards(),
        }

    def generate_report(self) -> dict[str, Any]:
        """Generate paper trading performance report."""
        eq = np.array(self.account.equity_history)
        if len(eq) < 2:
            return {"error": "Not enough data"}

        rets = np.diff(eq) / eq[:-1]
        ann_ret = float(np.mean(rets) * 252) if len(rets) > 0 else 0
        ann_vol = float(np.std(rets, ddof=1) * np.sqrt(252)) if len(rets) > 1 else 0
        sharpe = (ann_ret - 0.025) / ann_vol if ann_vol > 1e-8 else 0
        peak = np.maximum.accumulate(eq)
        mdd = float(np.min((eq - peak) / peak))

        return {
            "sharpe_ratio": round(sharpe, 4),
            "annual_return": round(ann_ret, 4),
            "annual_volatility": round(ann_vol, 4),
            "max_drawdown": round(mdd, 4),
            "total_return": round(eq[-1] / eq[0] - 1, 4),
            "days_traded": self.days_traded,
            "total_trades": len(self.account.trade_log),
        }
