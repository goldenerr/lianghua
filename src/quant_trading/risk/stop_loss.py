"""
Stop-loss / take-profit for individual positions.

Implements:
  - Trailing stop: exit when price drops X% from entry
  - Time stop: exit after N days regardless of P&L
  - Profit target: exit when gain exceeds Y%

Integrated into backtest loop as a position manager that overrides
the rebalance-driven target weights.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
import numpy as np


@dataclass
class Position:
    symbol: str
    entry_price: float
    entry_day: int
    weight: float
    peak_price: float = 0.0  # for trailing stop
    
    def __post_init__(self):
        if self.peak_price == 0.0:
            self.peak_price = self.entry_price


class StopLossManager:
    """Manages stop-loss and take-profit for a portfolio of positions.
    
    Checks at each bar (day):
      - Trailing stop: current_price < peak_price * (1 - trail_pct)
      - Hard stop: current_price < entry_price * (1 - hard_stop_pct)
      - Profit target: current_price > entry_price * (1 + profit_target_pct)
      - Time stop: days held > max_hold_days
    """
    
    def __init__(
        self,
        trail_stop_pct: float = 0.15,     # -15% from peak
        hard_stop_pct: float = 0.20,      # -20% from entry (hard floor)
        profit_target_pct: float = 0.50,  # +50% take profit
        max_hold_days: int = 252,         # exit after 1 year
        enable_trail: bool = True,
        enable_hard: bool = True,
        enable_profit: bool = False,      # disabled by default (let winners run)
        enable_time: bool = False,        # disabled by default
    ):
        self.trail_stop_pct = trail_stop_pct
        self.hard_stop_pct = hard_stop_pct
        self.profit_target_pct = profit_target_pct
        self.max_hold_days = max_hold_days
        self.enable_trail = enable_trail
        self.enable_hard = enable_hard
        self.enable_profit = enable_profit
        self.enable_time = enable_time
        
        self.positions: dict[str, Position] = {}
        self.exit_log: list[dict] = []  # record of exits
    
    def update_prices(self, prices: dict[str, float], day: int) -> set[str]:
        """Check all positions against current prices. Return symbols to exit."""
        exited = set()
        
        for sym in list(self.positions.keys()):
            if sym not in prices:
                continue
            
            pos = self.positions[sym]
            current = prices[sym]
            if current <= 0:
                continue
            
            # Update trailing peak
            if current > pos.peak_price:
                pos.peak_price = current
            
            exit_reason = None
            
            # Hard stop
            if self.enable_hard and current < pos.entry_price * (1 - self.hard_stop_pct):
                exit_reason = f"hard_stop:{self.hard_stop_pct:.0%}"
            
            # Trailing stop
            elif self.enable_trail and current < pos.peak_price * (1 - self.trail_stop_pct):
                exit_reason = f"trail_stop:{self.trail_stop_pct:.0%} from peak"
            
            # Profit target
            elif self.enable_profit and current > pos.entry_price * (1 + self.profit_target_pct):
                exit_reason = f"profit_target:{self.profit_target_pct:.0%}"
            
            # Time stop
            elif self.enable_time and (day - pos.entry_day) > self.max_hold_days:
                exit_reason = f"time_stop:{self.max_hold_days}d"
            
            if exit_reason:
                exited.add(sym)
                self.exit_log.append({
                    "symbol": sym,
                    "entry_day": pos.entry_day,
                    "exit_day": day,
                    "entry_price": round(pos.entry_price, 2),
                    "exit_price": round(current, 2),
                    "return": round(current / pos.entry_price - 1, 4),
                    "reason": exit_reason,
                })
                del self.positions[sym]
        
        return exited
    
    def add_positions(self, new_weights: dict[str, float], prices: dict[str, float], day: int):
        """Add new positions. Update entry for existing positions that increased weight."""
        for sym, w in new_weights.items():
            if sym not in prices or prices[sym] <= 0:
                continue
            if w <= 0:
                continue
            
            current = prices[sym]
            if sym in self.positions:
                # Position exists — update peak if needed
                pos = self.positions[sym]
                if current > pos.peak_price:
                    pos.peak_price = current
            else:
                # New position
                self.positions[sym] = Position(
                    symbol=sym,
                    entry_price=current,
                    entry_day=day,
                    weight=w,
                    peak_price=current,
                )
    
    def remove_positions(self, symbols: set[str]):
        """Remove positions (for rebalance exits, not stops)."""
        for sym in symbols:
            if sym in self.positions:
                del self.positions[sym]
                # Don't log — these are voluntary exits
    
    def exit_summary(self) -> dict:
        """Summary statistics of stop-loss exits."""
        if not self.exit_log:
            return {"total_exits": 0}
        
        returns = [e["return"] for e in self.exit_log]
        reasons = [e["reason"] for e in self.exit_log]
        
        return {
            "total_exits": len(self.exit_log),
            "avg_exit_return": round(float(np.mean(returns)), 4),
            "median_exit_return": round(float(np.median(returns)), 4),
            "worst_exit": round(float(np.min(returns)), 4),
            "by_reason": {r: reasons.count(r) for r in set(reasons)},
        }
    
    def current_positions(self) -> dict[str, float]:
        """Return current position weights."""
        return {sym: pos.weight for sym, pos in self.positions.items()}


# ── Test ─────────────────────────────────────────────────────
if __name__ == "__main__":
    sl = StopLossManager(
        trail_stop_pct=0.15,
        hard_stop_pct=0.20,
        profit_target_pct=0.50,
    )
    
    # Day 0: enter 2 positions
    sl.add_positions(
        {"AAPL": 0.5, "GOOG": 0.5},
        {"AAPL": 100.0, "GOOG": 200.0},
        day=0,
    )
    
    # Day 5: AAPL drops 16% from peak, GOOG up
    exited = sl.update_prices({"AAPL": 84.0, "GOOG": 220.0}, day=5)
    print(f"Day 5 exits: {exited}")  # AAPL should exit (16% from peak)
    
    # Day 10: GOOG drops 22% from entry
    sl.add_positions({"GOOG": 0.5}, {"GOOG": 156.0}, day=10)  # re-add
    exited = sl.update_prices({"GOOG": 156.0}, day=10)
    print(f"Day 10 exits: {exited}")  # GOOG should exit (22% hard stop)
    
    print(f"\nExit summary: {sl.exit_summary()}")
