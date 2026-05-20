#!/usr/bin/env python3.9
"""
V4.2 — V3.5 MR factors + Enhanced price-volume factors.
NO risk management overlay (vol targeting killed V4.1: Sharpe 0.24 vs 0.88).
Tests whether turnover/money_flow/VPT/rel_strength add alpha.
"""
from __future__ import annotations
import json, sys, time
from datetime import datetime
from pathlib import Path
from typing import Optional
import numpy as np
import pandas as pd

PROJECT = Path("/home/hermes/.hermes/projects/lianghua")
DATA_DIR = PROJECT / "data/parquet"
OUT_DIR = PROJECT / "data/backtest_results"
OUT_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(PROJECT / "src"))
from quant_trading.strategy.factors import composite_score

CONFIG = {
    "top_n": 30, "rebalance_freq": 60, "max_position_pct": 0.20,
    "warmup_days": 252, "test_warmup_days": 60,
    "min_stocks": 50, "min_history": 252,
    "stamp_duty": 0.0005, "commission": 0.00025,
    "slippage_base": 0.0005, "slippage_factor": 0.10,
    "risk_free_rate": 0.025,
    "n_folds": 5, "oos_pct": 0.20, "purge_days": 21,
}

# V3.5 weights (MR core) + enhanced price-volume
V42_WEIGHTS = {
    # Mean-reversion core (reduced slightly to make room)
    "rsi":          0.18,
    "bollinger":    0.18,
    "momentum":     0.14,
    "macd":         0.10,
    "vol_dev":      0.08,
    "low_vol":      0.05,
    # Price-volume enhanced
    "turnover":     0.10,
    "money_flow":   0.07,
    "vpt":          0.05,
    "rel_strength": 0.05,
}

# ── V3.5 EXACT Factor Functions ──────────────────────────────
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

# ── Enhanced Price-Volume Factor Functions ───────────────────
def factor_turnover_anomaly(volumes):
    """Turnover anomaly: recent / 20d avg - 1 (attention signal)."""
    if len(volumes) < 20: return np.nan
    recent = volumes[-5:].mean()
    base = volumes[-20:-5].mean()
    if base < 1e-12: return 0.0
    return recent / base - 1.0

def factor_money_flow(highs, lows, closes, volumes):
    """Chaikin Money Flow: buying/selling pressure, 5d window."""
    window = 5
    if len(closes) < window + 1: return np.nan
    h, l, c, v = highs[-window-1:], lows[-window-1:], closes[-window-1:], volumes[-window-1:]
    hl_diff = np.where(h - l < 1e-12, 1e-12, h - l)
    mfm = ((c - l) - (h - c)) / hl_diff
    mfv = mfm * v
    return float(np.sum(mfv[-window:]) / max(np.sum(v[-window:]), 1e-12))

def factor_vpt(closes, volumes):
    """Volume-Price Trend: cumulative vol * price change, 5d window."""
    window = 5
    if len(closes) < window + 1: return np.nan
    c, v = closes[-window-1:], volumes[-window-1:]
    pct = np.diff(c) / c[:-1]
    total_vol = np.sum(v[1:])
    if total_vol < 1e-12: return 0.0
    return float(np.sum(v[1:] * pct) / total_vol)

def factor_rel_strength(closes):
    """Relative strength: 10d return / abs(63d return)."""
    if len(closes) < 73: return np.nan
    recent = closes[-1] / closes[-10] - 1.0
    long_ret = closes[-1] / closes[-63] - 1.0
    if abs(long_ret) < 1e-12:
        return 0.0 if abs(recent) < 1e-12 else np.sign(recent)
    return float(recent / abs(long_ret))

# ── Factor Registry ──────────────────────────────────────────
FACTOR_REGISTRY = {
    # MR factors (close-only)
    "rsi":        (factor_rsi_mr,        ["close"]),
    "bollinger":  (factor_bollinger_mr,  ["close"]),
    "momentum":   (factor_momentum,      ["close"]),
    "macd":       (factor_macd,          ["close"]),
    "vol_dev":    (factor_vol_dev,       ["volume"]),
    "low_vol":    (factor_low_vol,       ["close"]),
    # Enhanced price-volume
    "turnover":   (factor_turnover_anomaly, ["volume"]),
    "money_flow": (factor_money_flow,    ["high", "low", "close", "volume"]),
    "vpt":        (factor_vpt,           ["close", "volume"]),
    "rel_strength": (factor_rel_strength, ["close"]),
}

def compute_factors(stock_data, factor_names=None):
    """Compute all requested factors for a single stock."""
    if factor_names is None: factor_names = list(V42_WEIGHTS.keys())
    result = {}
    for name in factor_names:
        fn, cols = FACTOR_REGISTRY[name]
        args = [stock_data[c] for c in cols]
        result[name] = fn(*args)
    return result

def rank_stocks(stock_data_dict, weights=None):
    """Rank all stocks by composite factor score."""
    if weights is None: weights = V42_WEIGHTS
    factor_names = list(weights.keys())
    
    # Compute raw factors for all stocks
    raw_factors = {}
    for sym, data in stock_data_dict.items():
        fv = compute_factors(data, factor_names)
        if not all(np.isnan(v) for v in fv.values()):
            raw_factors[sym] = fv
    
    if not raw_factors: return []
    
    # Cross-sectional z-score
    cross_z = {}
    for name in factor_names:
        vals = [fv.get(name, np.nan) for fv in raw_factors.values()]
        valid = [v for v in vals if not np.isnan(v)]
        if len(valid) >= 5:
            cross_z[name] = (float(np.mean(valid)), float(np.std(valid, ddof=1)))
    
    # Score and rank
    ranked = []
    for sym, fv in raw_factors.items():
        cs = composite_score(fv, weights, cross_z)
        if not np.isnan(cs):
            ranked.append((sym, cs))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked


# ── Data Loading ─────────────────────────────────────────────
def load():
    """Load all stock data: close, volume, high, low."""
    files = sorted(DATA_DIR.glob("*.parquet"))
    stocks = {}
    ml = 0
    print(f"Loading {len(files)} stocks...", flush=True)
    t0 = time.time()
    for i, f in enumerate(files):
        if (i+1) % 300 == 0:
            print(f"  [{i+1}/{len(files)}] {time.time()-t0:.0f}s", flush=True)
        try:
            df = pd.read_parquet(f)
            if len(df) < CONFIG["min_history"]: continue
            sym = f.stem
            stocks[sym] = {
                "close": df["close"].values.astype(np.float64),
                "volume": df["volume"].values.astype(np.float64),
                "high": df["high"].values.astype(np.float64),
                "low": df["low"].values.astype(np.float64),
            }
            ml = max(ml, len(df))
        except Exception:
            continue
    print(f"  {len(stocks)} stocks, max={ml}, {time.time()-t0:.0f}s", flush=True)
    return stocks, ml


# ── Backtest ─────────────────────────────────────────────────
def backtest(stocks, start_idx, end_idx, cfg, warmup=None):
    if warmup is None: warmup = cfg["warmup_days"]
    n_days = end_idx - start_idx
    if n_days < warmup + 10: return None
    top_n = min(cfg["top_n"], 20)
    rfreq = cfg["rebalance_freq"]
    
    valid = [s for s in sorted(stocks.keys()) if len(stocks[s]["close"]) > start_idx + warmup]
    if len(valid) < cfg["min_stocks"]: return None
    
    positions: dict[str, float] = {}
    daily_returns: list[float] = []
    turnover_rates: list[float] = []
    active_counts: list[int] = []
    
    for t in range(start_idx + warmup, end_idx - 1):
        if (t - start_idx - warmup) % rfreq == 0:
            snapshot = {}
            for sym in valid:
                d = stocks[sym]
                if t >= len(d["close"]): continue
                lookback = min(252, t + 1)
                start = t - lookback + 1
                p_slice = d["close"][start:t+1]
                if len(p_slice) < 60 or np.any(np.isnan(p_slice)): continue
                snapshot[sym] = {
                    "close": p_slice,
                    "volume": d["volume"][start:t+1],
                    "high": d["high"][start:t+1],
                    "low": d["low"][start:t+1],
                }
            
            active_counts.append(len(snapshot))
            if len(snapshot) < cfg["min_stocks"]:
                positions = {}
                daily_returns.append(0.0)
                continue
            
            ranked = rank_stocks(snapshot, V42_WEIGHTS)
            selected = set(sym for sym, _ in ranked[:top_n])
            n_sel = len(selected)
            if n_sel == 0:
                positions = {}
                daily_returns.append(0.0)
                continue
            
            w = min(1.0 / n_sel, cfg["max_position_pct"])
            if w * n_sel > 1.0: w = 1.0 / n_sel
            target_weights = {s: w for s in selected}
            
            all_keys = set(positions.keys()) | set(target_weights.keys())
            turnover = sum(abs(positions.get(s, 0) - target_weights.get(s, 0)) for s in all_keys)
            turnover_rates.append(turnover)
            slippage = cfg["slippage_base"] * (1 + cfg["slippage_factor"] * np.sqrt(turnover))
            positions = target_weights
        
        day_return = 0.0
        for sym, w in positions.items():
            if sym not in stocks or t + 1 >= len(stocks[sym]["close"]): continue
            p_t, p_t1 = stocks[sym]["close"][t], stocks[sym]["close"][t + 1]
            if p_t <= 0 or p_t1 <= 0: continue
            day_return += w * (p_t1 / p_t - 1.0)
        daily_returns.append(day_return)
    
    if len(daily_returns) < 50: return None
    
    rets = np.array(daily_returns)
    ann_ret = float(np.mean(rets) * 252)
    ann_vol = float(np.std(rets, ddof=1) * np.sqrt(252))
    sharpe = None
    if ann_vol > 1e-8:
        s = (ann_ret - cfg["risk_free_rate"]) / ann_vol
        if abs(s) < 100: sharpe = round(s, 4)
    
    equity = np.cumprod(1 + rets)
    peak = np.maximum.accumulate(equity)
    max_dd = round(float(np.min((equity - peak) / peak)), 4)
    calmar = round(ann_ret / abs(max_dd), 4) if abs(max_dd) > 1e-10 and ann_vol > 1e-8 else 0.0
    
    return {
        "sharpe_ratio": sharpe,
        "annual_return": round(ann_ret, 4),
        "annual_volatility": round(ann_vol, 4),
        "max_drawdown": max_dd,
        "calmar_ratio": calmar,
        "win_rate": round(float(np.mean(rets > 0)), 4),
        "total_return": round(float(equity[-1] - 1), 4),
        "n_rebalances": len(turnover_rates),
        "n_days": len(daily_returns),
        "avg_active_stocks": round(float(np.mean(active_counts)), 1) if active_counts else 0,
        "avg_turnover": round(float(np.mean(turnover_rates)), 4) if turnover_rates else 0,
    }


def walk_forward(stocks, ml, cfg):
    nf = cfg["n_folds"]
    too = int(ml * cfg["oos_pct"])
    fs = too // nf
    fts = ml - too
    pg = cfg["purge_days"]
    results = []
    is_sharpes = []
    oos_sharpes = []
    
    for fold in range(nf):
        ts = fts + fold * fs
        te = min(ts + fs, ml)
        tre = ts - pg
        
        t0 = time.time()
        tr = backtest(stocks, 0, tre, cfg)
        ter = backtest(stocks, tre, te, cfg, warmup=cfg["test_warmup_days"])
        elapsed = time.time() - t0
        
        fr = {"fold": fold, "train": tre, "test": te - ts}
        if tr and tr["sharpe_ratio"] is not None:
            fr["is"] = tr["sharpe_ratio"]
            is_sharpes.append(tr["sharpe_ratio"])
        else:
            fr["is"] = None
        
        if ter and ter["sharpe_ratio"] is not None:
            fr["oos"] = ter["sharpe_ratio"]
            fr["act"] = ter.get("avg_active_stocks", 0)
            oos_sharpes.append(ter["sharpe_ratio"])
        else:
            fr["oos"] = None
        
        results.append(fr)
        print(f"  F{fold}: IS={fr['is']} OOS={fr.get('oos')} ({elapsed:.0f}s) a={fr.get('act','?')}", flush=True)
    
    ai = float(np.mean(is_sharpes)) if is_sharpes else np.nan
    ao = float(np.mean(oos_sharpes)) if oos_sharpes else np.nan
    decay = (ai - ao) / abs(ai) if is_sharpes and oos_sharpes and abs(ai) > 0.001 else np.nan
    
    return {
        "folds": results,
        "avg_is_sharpe": round(ai, 4) if not np.isnan(ai) else None,
        "avg_oos_sharpe": round(ao, 4) if not np.isnan(ao) else None,
        "sharpe_decay": round(decay, 4) if not np.isnan(decay) else None,
    }


def main():
    t0 = time.time()
    stocks, ml = load()
    
    print(f"\nFull bt...", flush=True)
    t1 = time.time()
    full = backtest(stocks, 0, ml, CONFIG)
    print(f"  {time.time()-t1:.0f}s", flush=True)
    
    print(f"\nWF...", flush=True)
    t2 = time.time()
    wf = walk_forward(stocks, ml, CONFIG)
    print(f"  {time.time()-t2:.0f}s", flush=True)
    
    # Gates
    ov = [r["oos"] for r in wf["folds"] if r.get("oos") is not None]
    iv = [r["is"] for r in wf["folds"] if r.get("is") is not None]
    g = {}
    if full and full["sharpe_ratio"] is not None:
        g["S"] = full["sharpe_ratio"] >= 1.2
        g["M"] = full["max_drawdown"] >= -0.15
    else:
        g["S"] = g["M"] = False
    g["D"] = (wf["sharpe_decay"] is not None and wf["sharpe_decay"] <= 0.30)
    g["A"] = all(g.values())
    
    out_path = OUT_DIR / "wf_backtest_v4.2_report.json"
    with open(out_path, "w") as f:
        json.dump({
            "ts": datetime.now().isoformat(),
            "version": "V4.2",
            "desc": "V3.5 MR + enhanced price-volume factors",
            "cfg": CONFIG,
            "weights": V42_WEIGHTS,
            "full": full,
            "wf": wf,
            "gates": g,
            "elapsed": round(time.time() - t0, 1),
        }, f, indent=2, default=str)
    
    print(f"\n{'='*60}")
    print(f"  V4.2 — V3.5 + Enhanced Price-Volume Factors")
    print(f"  {len(stocks)} stocks, top-{CONFIG['top_n']}, {CONFIG['rebalance_freq']}d rebalance")
    print(f"{'='*60}")
    if full:
        s = full["sharpe_ratio"]
        print(f"  Full: Sharpe={'N/A' if s is None else f'{s:.4f}'} "
              f"Ret={full['annual_return']:.1%} Vol={full['annual_volatility']:.1%} "
              f"MDD={full['max_drawdown']:.1%}")
        print(f"  Active={full['avg_active_stocks']:.0f} Turnover={full['avg_turnover']:.1%}")
    for fld in wf["folds"]:
        iss = f"{fld['is']:.3f}" if fld.get("is") is not None else "N/A"
        oss = fld.get("oos", "N/A")
        oss = f"{oss:.3f}" if isinstance(oss, float) else oss
        print(f"  F{fld['fold']}: IS={iss} OOS={oss} a={fld.get('act','?')}")
    print(f"  IS:{wf['avg_is_sharpe']} OOS:{wf['avg_oos_sharpe']} D:{wf['sharpe_decay']}")
    print(f"  {'✅' if g['A'] else '❌'} S={'✅' if g['S'] else '❌'} M={'✅' if g['M'] else '❌'} D={'✅' if g['D'] else '❌'}")
    print(f"  ⏱ {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
