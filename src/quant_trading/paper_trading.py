"""
V5.9 Paper Trading Engine — AGENTS.md §7 & §9 Gate 2.
Simulates live trading with real market data, dynamic slippage, and MDD safeguards.
"""
from __future__ import annotations
import json, time, logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import numpy as np
import pandas as pd

log = logging.getLogger("paper_trading")

# ── V5.9 Production Config ──────────────────────────────────
V59_CONFIG = {
    "top_n": 35,
    "rebalance_freq": 90,
    "max_position_pct": 0.20,
    "max_per_sector": 5,
    "slippage_base": 0.0005,
    "slippage_factor": 0.10,
    "stamp_duty": 0.0005,
    "commission": 0.00025,
    "risk_free_rate": 0.025,
    "warmup_days": 252,
    "min_history": 252,
    "min_stocks": 50,
    # MDD safeguards (V5.9 tightened)
    "mdd_reduce_threshold": 0.10,
    "mdd_reduce_scale": 0.50,
    "mdd_stop_threshold": 0.18,
    "mdd_stop_scale": 0.0,
}

V35_WEIGHTS = {"rsi":0.25,"bollinger":0.25,"momentum":0.20,"macd":0.15,"vol_dev":0.10,"low_vol":0.05}

# ── Factor Functions (V3.5 exact) ─────────────────────────
def factor_rsi_mr(closes):
    if len(closes)<15: return np.nan
    d=np.diff(closes[-15:]); g=np.clip(d,0,None).mean(); l=-np.clip(d,None,0).mean()
    if l<1e-12: return np.nan
    return abs(100.0-100.0/(1.0+g/l)-50.0)
def factor_bollinger_mr(closes):
    if len(closes)<20: return np.nan
    m=closes[-20:].mean(); s=closes[-20:].std(ddof=1); w=4.0*s
    return abs(closes[-1]-m)/w if w>1e-12 else 0.0
def factor_momentum(closes):
    if len(closes)<68: return np.nan
    return float(closes[-1]/closes[-68]-1)
def _ema(s,span):
    if len(s)<span: return np.nan
    a=2.0/(span+1); r=s[:span].mean()
    for i in range(span,len(s)): r=a*s[i]+(1-a)*r
    return r
def factor_macd(closes):
    if len(closes)<35: return np.nan
    return _ema(closes,12)-_ema(closes,26)
def factor_vol_dev(volumes):
    if len(volumes)<20: return np.nan
    m=volumes[-20:].mean()
    return -abs(volumes[-1]/m-1.0) if m>1e-12 else 0.0
def factor_low_vol(closes):
    if len(closes)<61: return np.nan
    r=np.diff(closes[-61:])/closes[-61:-1]
    return -np.std(r,ddof=1)*np.sqrt(252)

MR = {"rsi":(factor_rsi_mr,"close"),"bollinger":(factor_bollinger_mr,"close"),
      "momentum":(factor_momentum,"close"),"macd":(factor_macd,"close"),
      "vol_dev":(factor_vol_dev,"volume"),"low_vol":(factor_low_vol,"close")}


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
    limit_price: Optional[float] = None
    status: str = "pending"
    filled_qty: int = 0
    filled_price: float = 0.0
    created_at: str = ""
    client_id: str = ""
    
    def __post_init__(self):
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.client_id = f"paper_{self.symbol}_{int(time.time()*1e6)}"

@dataclass
class PaperAccount:
    cash: float
    initial_capital: float
    positions: dict[str, PaperPosition] = field(default_factory=dict)
    equity_history: list[float] = field(default_factory=list)
    trade_log: list[dict] = field(default_factory=list)
    peak_equity: float = 0.0
    
    def __post_init__(self):
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
    
    def update_market_values(self, prices: dict[str, float]):
        for sym, pos in self.positions.items():
            if sym in prices:
                pos.market_value = pos.shares * prices[sym]
        self.peak_equity = max(self.peak_equity, self.total_equity)
        self.equity_history.append(self.total_equity)


class PaperTradingEngine:
    """Paper trading engine for V5.9 strategy validation (AGENTS.md §9 Gate 2)."""
    
    def __init__(self, initial_capital: float = 1_000_000, config: dict = None):
        self.config = config or V59_CONFIG
        self.account = PaperAccount(cash=initial_capital, initial_capital=initial_capital)
        self.orders: list[PaperOrder] = []
        self.days_traded = 0
        self.last_rebalance_day = -999
        self.stopped = False
        self.industry_map: dict[str, str] = {}
        self._load_industries()
    
    def _load_industries(self):
        """Load industry classification for sector caps."""
        try:
            df = pd.read_parquet("/home/hermes/.hermes/projects/lianghua/data/industry_fixed.parquet")
            for _, row in df.iterrows():
                code = str(row['code'])
                ind = row.get('industry', '')
                if ind and not pd.isna(ind) and '.' in code:
                    self.industry_map[code.split('.')[1]] = ind
        except Exception as e:
            log.warning(f"Industry data load failed: {e}")
    
    def check_mdd_safeguards(self) -> str:
        """Check and apply MDD safeguards. Returns action taken."""
        dd = self.account.current_drawdown
        cfg = self.config
        
        if dd < -cfg.get("mdd_stop_threshold", 0.18):
            # Stop all trading
            self.stopped = True
            return f"STOPPED: DD={dd:.1%} exceeds stop threshold {cfg['mdd_stop_threshold']:.0%}"
        elif dd < -cfg.get("mdd_reduce_threshold", 0.10):
            return f"REDUCED: DD={dd:.1%} exceeds reduce threshold {cfg['mdd_reduce_threshold']:.0%}"
        return "OK"
    
    def compute_positions(self, snapshot: dict, current_date) -> dict[str, float]:
        """Compute target weights using V5.9 factor model."""
        from quant_trading.strategy.factors import composite_score
        return self._compute_positions_inline(snapshot)
    
    def _compute_positions_inline(self, snapshot: dict) -> dict[str, float]:
        """Inline factor computation (avoids import issues)."""
        cfg = self.config
        weights = V35_WEIGHTS
        factor_names = list(weights.keys())
        
        raw = {}
        for sym, data in snapshot.items():
            fv = {}
            for name in factor_names:
                fn, dt = MR[name]
                arr = np.asarray(data["close"] if dt == "close" else data.get("volume", []))
                fv[name] = fn(arr)
            if not all(np.isnan(v) for v in fv.values()):
                raw[sym] = fv
        
        if not raw: return {}
        
        # Cross-sectional z-score
        cz = {}
        for name in factor_names:
            vals = [fv.get(name, np.nan) for fv in raw.values()]
            valid = [v for v in vals if not np.isnan(v)]
            if len(valid) >= 5:
                cz[name] = (float(np.mean(valid)), float(np.std(valid, ddof=1)))
        
        # Score and rank
        from quant_trading.strategy.factors import composite_score
        ranked = []
        for sym, fv in raw.items():
            cs = composite_score(fv, weights, cz)
            if not np.isnan(cs):
                ranked.append((sym, cs))
        ranked.sort(key=lambda x: x[1], reverse=True)
        
        # Apply sector caps
        top_n = cfg["top_n"]
        max_sec = cfg.get("max_per_sector", 5)
        selected = []
        sec_counts = {}
        for sym, score in ranked:
            ind = self.industry_map.get(sym, "__UNKNOWN__")
            if sec_counts.get(ind, 0) < max_sec:
                selected.append(sym)
                sec_counts[ind] = sec_counts.get(ind, 0) + 1
            if len(selected) >= top_n:
                break
        
        if not selected:
            return {}
        
        n = len(selected)
        w = min(1.0 / n, cfg["max_position_pct"])
        if w * n > 1.0:
            w = 1.0 / n
        
        # Apply MDD safeguards
        dd = self.account.current_drawdown
        if dd < -cfg.get("mdd_stop_threshold", 0.18):
            return {}  # Stop all
        elif dd < -cfg.get("mdd_reduce_threshold", 0.10):
            w *= cfg.get("mdd_reduce_scale", 0.50)
        
        return {s: w for s in selected}
    
    def execute_rebalance(self, target_weights: dict[str, float], prices: dict[str, float], date_str: str):
        """Execute orders to reach target weights. Simulates slippage + costs."""
        cfg = self.config
        
        # Calculate current weights
        total_eq = self.account.total_equity
        current_weights = {}
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
                slippage = cfg["slippage_base"] * (1 + cfg["slippage_factor"] * abs(diff))
                exec_price = price * (1 + slippage)
                cost = target_value - current_value
                cost_with_fees = cost * (1 + cfg["stamp_duty"] + cfg["commission"])
                
                if cost_with_fees <= self.account.cash:
                    shares = int(target_value / exec_price / 100) * 100  # Round to lots
                    if shares > 0:
                        actual_cost = shares * exec_price * (1 + cfg["stamp_duty"] + cfg["commission"])
                        if actual_cost <= self.account.cash:
                            self.account.cash -= actual_cost
                            if sym not in self.account.positions:
                                self.account.positions[sym] = PaperPosition(symbol=sym)
                            pos = self.account.positions[sym]
                            pos.shares += shares
                            pos.avg_cost = ((pos.avg_cost * (pos.shares - shares)) + actual_cost) / pos.shares if pos.shares > 0 else exec_price
                            
                            self.account.trade_log.append({
                                "date": date_str, "symbol": sym, "side": "buy",
                                "shares": shares, "price": round(exec_price, 2),
                                "cost": round(actual_cost, 2), "slippage": round(slippage, 4)
                            })
            
            elif diff < 0:  # Sell
                pos = self.account.positions.get(sym)
                if not pos or pos.shares <= 0:
                    continue
                
                slippage = cfg["slippage_base"] * (1 + cfg["slippage_factor"] * abs(diff))
                exec_price = price * (1 - slippage)
                sell_value = abs(current_value - target_value)
                shares_to_sell = min(pos.shares, int(sell_value / exec_price / 100) * 100)
                
                if shares_to_sell > 0:
                    proceeds = shares_to_sell * exec_price * (1 - cfg["stamp_duty"] - cfg["commission"])
                    self.account.cash += proceeds
                    pos.shares -= shares_to_sell
                    if pos.shares <= 0:
                        del self.account.positions[sym]
                    
                    self.account.trade_log.append({
                        "date": date_str, "symbol": sym, "side": "sell",
                        "shares": shares_to_sell, "price": round(exec_price, 2),
                        "proceeds": round(proceeds, 2), "slippage": round(slippage, 4)
                    })
        
        # Sell positions not in targets
        for sym in list(self.account.positions.keys()):
            if sym not in target_weights:
                pos = self.account.positions[sym]
                if pos.shares <= 0:
                    continue
                price = prices.get(sym, 0)
                if price <= 0:
                    continue
                exec_price = price * (1 - cfg["slippage_base"])
                proceeds = pos.shares * exec_price * (1 - cfg["stamp_duty"] - cfg["commission"])
                self.account.cash += proceeds
                self.account.trade_log.append({
                    "date": date_str, "symbol": sym, "side": "sell",
                    "shares": pos.shares, "price": round(exec_price, 2),
                    "proceeds": round(proceeds, 2), "reason": "removed from portfolio"
                })
                del self.account.positions[sym]
    
    def get_status(self) -> dict:
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
    
    def generate_report(self) -> dict:
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
