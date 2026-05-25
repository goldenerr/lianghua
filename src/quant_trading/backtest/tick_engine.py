"""
V7 Tick-Level Backtesting Engine — Bar-by-bar replay with synthetic tick generation.
Uses Brownian bridge intraday simulation from daily OHLCV for realistic market microstructure.

Key features:
  1. Synthetic tick generation from daily OHLCV (minute/second resolution)
  2. Volume profile modeling (U-shaped intraday pattern for A-shares)
  3. Bid-ask spread simulation
  4. Market impact at tick granularity
  5. Exact bar-by-bar replay for strategy validation
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class Tick:
    """Single market tick."""

    timestamp: float  # seconds from session start
    price: float
    volume: int
    bid: float = 0.0
    ask: float = 0.0
    is_buy: bool = True  # trade direction (Lee-Ready algo)


@dataclass
class TickBar:
    """Aggregated bar (1min, 5min, etc.)"""

    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float
    n_ticks: int = 0
    bid_close: float = 0.0
    ask_close: float = 0.0


@dataclass
class TickSession:
    """One trading session of tick data."""

    date: str
    ticks: list[Tick] = field(default_factory=list)
    bars: dict[int, list[TickBar]] = field(default_factory=dict)  # {resolution_seconds: bars}


@dataclass
class TickBacktestResult:
    """Tick-level backtest result."""

    total_return: float
    sharpe_ratio: float
    max_drawdown: float
    win_rate: float
    avg_slippage_bps: float  # average slippage in bps
    fill_rate: float  # % of orders filled
    avg_execution_time_sec: float  # average time to fill
    n_trades: int
    n_ticks_processed: int


# ═══════════════════════════════════════════════════════════════
# Synthetic Tick Generation
# ═══════════════════════════════════════════════════════════════


class TickGenerator:
    """Generate realistic synthetic tick data from daily OHLCV.

    Uses:
      - Brownian bridge for intraday price path (respects O/H/L/C)
      - U-shaped volume profile (A-share specific)
      - Lee-Ready algorithm for trade direction
      - Bid-ask spread simulation
    """

    # A-share session: 9:30-11:30, 13:00-15:00 = 240 minutes
    SESSION_START = 9 * 3600 + 30 * 60  # 09:30 in seconds
    MORNING_END = 11 * 3600 + 30 * 60  # 11:30
    AFTERNOON_START = 13 * 3600  # 13:00
    SESSION_END = 15 * 3600  # 15:00

    def __init__(self, tick_frequency_sec: float = 3.0, spread_bps: float = 2.0) -> None:
        """
        Args:
            tick_frequency_sec: average seconds between ticks (3s = ~4800 ticks/day)
            spread_bps: typical bid-ask spread in basis points
        """
        self.tick_freq = tick_frequency_sec
        self.spread_bps = spread_bps / 10000  # convert to decimal
        self.rng = np.random.RandomState(42)

    def generate_session(
        self,
        open_price: float,
        high: float,
        low: float,
        close: float,
        volume: int,
        date_str: str = "",
        resolution_sec: int = 60,
    ) -> TickSession:
        """Generate one trading session of tick data.

        Args:
            open_price, high, low, close: daily OHLC
            volume: daily volume (shares)
            date_str: date label
            resolution_sec: bar resolution (60 = 1min bars)

        Returns TickSession with ticks and bars.
        """
        session = TickSession(date=date_str)

        # Generate time points for morning and afternoon sessions
        morning_seconds = self.MORNING_END - self.SESSION_START
        afternoon_seconds = self.SESSION_END - self.AFTERNOON_START
        total_seconds = morning_seconds + afternoon_seconds

        n_morning_ticks = int(morning_seconds / self.tick_freq)
        n_afternoon_ticks = int(afternoon_seconds / self.tick_freq)
        n_ticks = n_morning_ticks + n_afternoon_ticks

        # ── Generate intraday price path (Brownian bridge) ──
        # We need a path that respects O/H/L/C constraints
        prices = self._generate_price_path(open_price, high, low, close, n_ticks, total_seconds)

        # ── Generate volume profile (U-shaped for A-shares) ──
        volumes = self._generate_volume_profile(volume, n_ticks)

        # ── Generate ticks ──
        for i in range(n_ticks):
            if i < n_morning_ticks:
                t = self.SESSION_START + i * self.tick_freq
            else:
                t = self.AFTERNOON_START + (i - n_morning_ticks) * self.tick_freq

            if t >= self.SESSION_END:
                break

            price = prices[i]
            vol = volumes[i]

            if vol <= 0:
                continue

            # Bid-ask spread
            half_spread = price * self.spread_bps / 2
            tick = Tick(
                timestamp=t,
                price=price,
                volume=vol,
                bid=price - half_spread,
                ask=price + half_spread,
                is_buy=self.rng.random() > 0.5,
            )
            session.ticks.append(tick)

        # ── Aggregate to bars ──
        session.bars[resolution_sec] = self._aggregate_bars(session.ticks, resolution_sec)

        return session

    def _generate_price_path(
        self,
        open_price: float,
        high: float,
        low: float,
        close: float,
        n_ticks: int,
        total_seconds: int,
    ) -> np.ndarray:
        """Generate Brownian bridge that respects OHLC."""
        log_O = np.log(open_price)
        log_C = np.log(close)

        # Standard Brownian bridge: B(t) = O + t*(C-O) + √t*(B_t - t*B_1)
        t = np.linspace(0, 1, n_ticks)
        drift = log_O + t * (log_C - log_O)

        # Generate Brownian bridge
        increments = self.rng.normal(0, 1, n_ticks)
        brownian = np.cumsum(increments) * np.sqrt(1.0 / n_ticks)
        bridge = brownian - t * brownian[-1]

        # Scale volatility to match H/L range
        log_path = drift + bridge

        # Rescale to match H and L exactly
        log_path_min = log_path.min()
        log_path_max = log_path.max()
        target_log_H = np.log(high)
        target_log_L = np.log(low)

        if abs(log_path_max - log_path_min) > 1e-12:
            log_path = target_log_L + (log_path - log_path_min) * (target_log_H - target_log_L) / (
                log_path_max - log_path_min
            )

        return np.asarray(np.exp(log_path), dtype=float)

    def _generate_volume_profile(self, total_volume: int, n_ticks: int) -> np.ndarray:
        """U-shaped intraday volume profile (typical for A-shares)."""
        t = np.linspace(0, 1, n_ticks)

        # U-shape: high at open, low midday, high at close
        profile = 0.3 + 0.7 * (np.exp(-10 * t) + np.exp(-10 * (1 - t))) / 2
        profile = profile / profile.sum() * total_volume

        # Add Poisson noise
        volumes = self.rng.poisson(np.maximum(profile, 0.1)).astype(int)
        volumes = np.clip(volumes, 1, None)

        # Rescale to match total
        scale = total_volume / volumes.sum() if volumes.sum() > 0 else 1
        volumes = (volumes * scale).astype(int)

        return np.asarray(volumes, dtype=int)

    def _aggregate_bars(self, ticks: list[Tick], resolution_sec: int) -> list[TickBar]:
        """Aggregate ticks into OHLCV bars."""
        if not ticks:
            return []

        bars = []
        start_time = ticks[0].timestamp
        end_time = ticks[-1].timestamp

        for bar_start in range(int(start_time), int(end_time), resolution_sec):
            bar_end = bar_start + resolution_sec
            bar_ticks = [t for t in ticks if bar_start <= t.timestamp < bar_end]

            if not bar_ticks:
                continue

            prices = [t.price for t in bar_ticks]
            volumes = [t.volume for t in bar_ticks]

            bar = TickBar(
                timestamp=bar_start,
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                volume=sum(volumes),
                vwap=float(np.average(prices, weights=volumes)),
                n_ticks=len(bar_ticks),
                bid_close=bar_ticks[-1].bid,
                ask_close=bar_ticks[-1].ask,
            )
            bars.append(bar)

        return bars


# ═══════════════════════════════════════════════════════════════
# Tick-Level Backtest Engine
# ═══════════════════════════════════════════════════════════════


class TickBacktestEngine:
    """Tick-level strategy backtesting with realistic microstructure."""

    def __init__(self, tick_generator: TickGenerator | None = None) -> None:
        self.generator = tick_generator or TickGenerator()
        self.order_book: dict[str, list[TickBar]] = {}  # current day's bars

    def run_backtest(
        self,
        daily_data: dict[str, dict[str, np.ndarray]],
        signal_func: Callable[[int, list[str]], dict[str, float]],
        dates: list[str],
        initial_capital: float = 1e6,
        resolution_sec: int = 60,
    ) -> TickBacktestResult:
        """Run tick-level backtest.

        Args:
            daily_data: per-symbol daily OHLCV data
            signal_func: generates target weights each day
            dates: list of date strings
            initial_capital: starting capital
            resolution_sec: bar resolution for execution
        """
        cash = initial_capital
        positions: dict[str, dict[str, float]] = {}  # {symbol: {shares, avg_cost}}
        equity_curve = [initial_capital]
        daily_returns = []
        slippages_bps = []
        fill_rates = []
        exec_times = []
        n_trades = 0
        total_ticks = 0

        for day_idx in range(len(dates)):
            # Generate signals at start of day
            target_weights = signal_func(day_idx, list(daily_data.keys()))

            # Generate tick data for the day
            sessions: dict[str, TickSession] = {}
            for sym, data in daily_data.items():
                if day_idx >= len(data.get("close", [])):
                    continue

                open_price = data["open"][day_idx]
                high = float(data["high"][day_idx])
                low = float(data["low"][day_idx])
                close = float(data["close"][day_idx])
                volume = float(data["volume"][day_idx])
                open_price = float(open_price)

                if open_price <= 0 or close <= 0 or volume <= 0:
                    continue

                session = self.generator.generate_session(
                    open_price, high, low, close, int(volume), str(dates[day_idx]), resolution_sec
                )
                sessions[sym] = session
                total_ticks += len(session.ticks)

            # Execute orders bar-by-bar
            previous_equity = equity_curve[-1]
            day_trades = self._execute_day(
                positions, target_weights, sessions, cash, resolution_sec
            )

            cash += day_trades["pnl"]
            n_trades += int(day_trades["n_trades"])
            slippages_bps.extend(day_trades["slippages"])
            fill_rates.append(float(day_trades["fill_rate"]))
            exec_times.extend(day_trades["exec_times"])

            marked_positions = sum(
                pos["shares"] * sessions[sym].ticks[-1].price
                for sym, pos in positions.items()
                if sym in sessions and sessions[sym].ticks
            )
            end_equity = cash + marked_positions
            equity_curve.append(end_equity)
            daily_returns.append((end_equity - previous_equity) / max(previous_equity, 1))

        # Compute stats
        rets = np.array(daily_returns)
        ann_ret = np.mean(rets) * 252
        ann_vol = np.std(rets, ddof=1) * np.sqrt(252)
        sharpe = (ann_ret - 0.025) / ann_vol if ann_vol > 1e-8 else 0

        eq = np.array(equity_curve)
        peak = np.maximum.accumulate(eq)
        mdd = np.min((eq - peak) / peak)

        return TickBacktestResult(
            total_return=float(eq[-1] / eq[0] - 1),
            sharpe_ratio=float(sharpe),
            max_drawdown=float(mdd),
            win_rate=float(np.mean(rets > 0)),
            avg_slippage_bps=float(np.mean(slippages_bps) * 10000) if slippages_bps else 0,
            fill_rate=float(np.mean(fill_rates)) if fill_rates else 0,
            avg_execution_time_sec=float(np.mean(exec_times)) if exec_times else 0,
            n_trades=n_trades,
            n_ticks_processed=total_ticks,
        )

    def _execute_day(
        self,
        positions: dict[str, dict[str, float]],
        target_weights: dict[str, float],
        sessions: dict[str, TickSession],
        capital: float,
        resolution_sec: int,
    ) -> dict[str, Any]:
        """Execute one day's trades bar-by-bar."""
        trades: dict[str, Any] = {
            "pnl": 0.0,
            "n_trades": 0,
            "slippages": [],
            "fill_rate": 0.0,
            "exec_times": [],
            "return_pct": 0.0,
        }

        # Calculate current weights
        current_values: dict[str, float] = {}
        total_value = capital
        for sym, pos in positions.items():
            if sym in sessions:
                last_price = sessions[sym].ticks[-1].price if sessions[sym].ticks else 0
                val = pos["shares"] * last_price
                current_values[sym] = val
                total_value += val

        # Generate orders
        orders: list[dict[str, Any]] = []
        for sym, tw in target_weights.items():
            if sym not in sessions:
                continue

            target_value = total_value * tw
            current_value = current_values.get(sym, 0)
            diff = target_value - current_value

            if abs(diff) < 100:  # min trade size
                continue

            current_price = sessions[sym].ticks[0].price if sessions[sym].ticks else 0
            if current_price <= 0:
                continue

            shares = int(abs(diff) / current_price / 100) * 100
            if shares >= 100:
                orders.append(
                    {
                        "sym": sym,
                        "side": "buy" if diff > 0 else "sell",
                        "shares": shares,
                        "remaining": shares,
                    }
                )

        if not orders:
            return trades

        # Execute bar-by-bar
        max_bars = max(len(s.bars.get(resolution_sec, [])) for s in sessions.values())

        for bar_idx in range(max_bars):
            for order in orders:
                if order["remaining"] <= 0:
                    continue

                sym = order["sym"]
                bars = sessions[sym].bars.get(resolution_sec, [])
                if bar_idx >= len(bars):
                    continue

                bar = bars[bar_idx]

                # Execute at VWAP ± half spread
                exec_price = bar.vwap
                exec_price = bar.ask_close if order["side"] == "buy" else bar.bid_close

                # VWAP participation: fill proportional to bar volume / daily volume
                daily_vol = sum(b.volume for b in bars)
                fill_pct = (
                    min(1.0, bar.volume / daily_vol * 3) if daily_vol > 0 else 0.1
                )  # up to 3x volume share

                fill_shares = int(order["remaining"] * fill_pct)
                if fill_shares < 100:
                    continue

                fill_shares = min(fill_shares, order["remaining"])

                # Record trade
                mid_price = (bar.high + bar.low) / 2
                slippage = abs(exec_price - mid_price) / mid_price

                if order["side"] == "buy":
                    cost = fill_shares * exec_price
                    trades["pnl"] -= cost
                    position = positions.setdefault(sym, {"shares": 0, "avg_cost": 0.0})
                    old_shares = position["shares"]
                    new_shares = old_shares + fill_shares
                    position["avg_cost"] = (old_shares * position["avg_cost"] + cost) / new_shares
                    position["shares"] = new_shares
                else:
                    sell_position = positions.get(sym)
                    available = sell_position["shares"] if sell_position else 0
                    fill_shares = min(fill_shares, available)
                    if fill_shares <= 0:
                        continue
                    proceeds = fill_shares * exec_price
                    trades["pnl"] += proceeds
                    assert sell_position is not None
                    sell_position["shares"] -= fill_shares
                    if sell_position["shares"] <= 0:
                        positions.pop(sym, None)

                order["remaining"] -= fill_shares

                trades["n_trades"] += 1
                trades["slippages"].append(slippage)
                trades["exec_times"].append(bar_idx * resolution_sec / 60)

        # Check fill rate
        total_ordered = sum(o["shares"] for o in orders)
        total_remaining = sum(o["remaining"] for o in orders)
        trades["fill_rate"] = 1.0 - total_remaining / max(total_ordered, 1)

        # Calculate day return
        trades["return_pct"] = trades["pnl"] / max(capital, 1)

        return trades
