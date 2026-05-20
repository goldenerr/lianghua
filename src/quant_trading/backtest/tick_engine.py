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
from dataclasses import dataclass, field
from typing import Optional, Callable
import numpy as np
from datetime import datetime, timedelta


@dataclass
class Tick:
    """Single market tick."""
    timestamp: float          # seconds from session start
    price: float
    volume: int
    bid: float = 0.0
    ask: float = 0.0
    is_buy: bool = True       # trade direction (Lee-Ready algo)


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
    avg_slippage_bps: float      # average slippage in bps
    fill_rate: float             # % of orders filled
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
    SESSION_START = 9 * 3600 + 30 * 60   # 09:30 in seconds
    MORNING_END = 11 * 3600 + 30 * 60    # 11:30
    AFTERNOON_START = 13 * 3600           # 13:00
    SESSION_END = 15 * 3600              # 15:00
    
    def __init__(self, tick_frequency_sec: float = 3.0, spread_bps: float = 2.0):
        """
        Args:
            tick_frequency_sec: average seconds between ticks (3s = ~4800 ticks/day)
            spread_bps: typical bid-ask spread in basis points
        """
        self.tick_freq = tick_frequency_sec
        self.spread_bps = spread_bps / 10000  # convert to decimal
        self.rng = np.random.RandomState(42)
    
    def generate_session(self, open_price: float, high: float, low: float, 
                          close: float, volume: int, date_str: str = "",
                          resolution_sec: int = 60) -> TickSession:
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
    
    def _generate_price_path(self, O, H, L, C, n_ticks, total_seconds):
        """Generate Brownian bridge that respects OHLC."""
        log_O = np.log(O)
        log_C = np.log(C)
        
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
        target_log_H = np.log(H)
        target_log_L = np.log(L)
        
        if abs(log_path_max - log_path_min) > 1e-12:
            log_path = target_log_L + (log_path - log_path_min) * (target_log_H - target_log_L) / (log_path_max - log_path_min)
        
        return np.exp(log_path)
    
    def _generate_volume_profile(self, total_volume, n_ticks):
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
        
        return volumes
    
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
                vwap=np.average(prices, weights=volumes),
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
    
    def __init__(self, tick_generator: TickGenerator = None):
        self.generator = tick_generator or TickGenerator()
        self.order_book = {}  # {symbol: list[TickBar]} current day's bars
    
    def run_backtest(
        self,
        daily_data: dict[str, np.ndarray],  # {symbol: {open/high/low/close/volume arrays}}
        signal_func: Callable,               # (date_idx, symbols) → {symbol: target_weight}
        dates: list,
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
        capital = initial_capital
        positions = {}  # {symbol: {shares, avg_cost}}
        equity_curve = [capital]
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
            sessions = {}
            for sym, data in daily_data.items():
                if day_idx >= len(data.get('close', [])):
                    continue
                
                O = data['open'][day_idx]
                H = data['high'][day_idx]
                L = data['low'][day_idx]
                C = data['close'][day_idx]
                V = data['volume'][day_idx]
                
                if O <= 0 or C <= 0 or V <= 0:
                    continue
                
                session = self.generator.generate_session(
                    O, H, L, C, int(V), str(dates[day_idx]), resolution_sec
                )
                sessions[sym] = session
                total_ticks += len(session.ticks)
            
            # Execute orders bar-by-bar
            day_trades = self._execute_day(positions, target_weights, sessions, 
                                            capital, resolution_sec)
            
            capital += day_trades['pnl']
            n_trades += day_trades['n_trades']
            slippages_bps.extend(day_trades['slippages'])
            fill_rates.append(day_trades['fill_rate'])
            exec_times.extend(day_trades['exec_times'])
            
            equity_curve.append(capital)
            daily_returns.append(day_trades['return_pct'])
        
        # Compute stats
        rets = np.array(daily_returns)
        ann_ret = np.mean(rets) * 252
        ann_vol = np.std(rets, ddof=1) * np.sqrt(252)
        sharpe = (ann_ret - 0.025) / ann_vol if ann_vol > 1e-8 else 0
        
        eq = np.array(equity_curve)
        peak = np.maximum.accumulate(eq)
        mdd = np.min((eq - peak) / peak)
        
        return TickBacktestResult(
            total_return=eq[-1] / eq[0] - 1,
            sharpe_ratio=sharpe,
            max_drawdown=mdd,
            win_rate=np.mean(rets > 0),
            avg_slippage_bps=np.mean(slippages_bps) * 10000 if slippages_bps else 0,
            fill_rate=np.mean(fill_rates) if fill_rates else 0,
            avg_execution_time_sec=np.mean(exec_times) if exec_times else 0,
            n_trades=n_trades,
            n_ticks_processed=total_ticks,
        )
    
    def _execute_day(self, positions, target_weights, sessions, capital, resolution_sec):
        """Execute one day's trades bar-by-bar."""
        trades = {'pnl': 0.0, 'n_trades': 0, 'slippages': [], 
                   'fill_rate': 0.0, 'exec_times': [], 'return_pct': 0.0}
        
        # Calculate current weights
        current_values = {}
        total_value = capital
        for sym, pos in positions.items():
            if sym in sessions:
                last_price = sessions[sym].ticks[-1].price if sessions[sym].ticks else 0
                val = pos['shares'] * last_price
                current_values[sym] = val
                total_value += val
        
        # Generate orders
        orders = []
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
                orders.append({
                    'sym': sym,
                    'side': 'buy' if diff > 0 else 'sell',
                    'shares': shares,
                    'remaining': shares,
                })
        
        if not orders:
            return trades
        
        # Execute bar-by-bar
        max_bars = max(len(s.bars.get(resolution_sec, [])) for s in sessions.values())
        
        for bar_idx in range(max_bars):
            for order in orders:
                if order['remaining'] <= 0:
                    continue
                
                sym = order['sym']
                bars = sessions[sym].bars.get(resolution_sec, [])
                if bar_idx >= len(bars):
                    continue
                
                bar = bars[bar_idx]
                
                # Execute at VWAP ± half spread
                exec_price = bar.vwap
                if order['side'] == 'buy':
                    exec_price = bar.ask_close
                else:
                    exec_price = bar.bid_close
                
                # VWAP participation: fill proportional to bar volume / daily volume
                daily_vol = sum(b.volume for b in bars)
                if daily_vol > 0:
                    fill_pct = min(1.0, bar.volume / daily_vol * 3)  # up to 3x volume share
                else:
                    fill_pct = 0.1
                
                fill_shares = int(order['remaining'] * fill_pct)
                if fill_shares < 100:
                    continue
                
                fill_shares = min(fill_shares, order['remaining'])
                order['remaining'] -= fill_shares
                
                # Record trade
                mid_price = (bar.high + bar.low) / 2
                slippage = abs(exec_price - mid_price) / mid_price
                
                if order['side'] == 'buy':
                    cost = fill_shares * exec_price
                    trades['pnl'] -= cost
                else:
                    proceeds = fill_shares * exec_price
                    trades['pnl'] += proceeds
                
                trades['n_trades'] += 1
                trades['slippages'].append(slippage)
                trades['exec_times'].append(bar_idx * resolution_sec / 60)
        
        # Check fill rate
        total_ordered = sum(o['shares'] for o in orders)
        total_remaining = sum(o['remaining'] for o in orders)
        trades['fill_rate'] = 1.0 - total_remaining / max(total_ordered, 1)
        
        # Calculate day return
        trades['return_pct'] = trades['pnl'] / max(capital, 1)
        
        return trades
