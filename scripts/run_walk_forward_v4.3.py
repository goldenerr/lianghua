#!/usr/bin/env python3.9
"""
V4.3 — V3.5 MR factors + Fundamental factors (PE/PB/PS).
Tests whether valuation factors add alpha beyond pure MR.
Price-volume factors excluded (they degraded V4.2).
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
FUND_DIR = PROJECT / "data/fundamentals"
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

# V3.5 MR core + fundamental (PE/PB/PS at 15% total, light touch)
V43_WEIGHTS = {
    "rsi":          0.22,
    "bollinger":    0.22,
    "momentum":     0.16,
    "macd":         0.10,
    "vol_dev":      0.08,
    "low_vol":      0.05,
    # Fundamental (15% total)
    "pe":           0.06,
    "pb":           0.05,
    "ps":           0.04,
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

# ── Fundamental Factor Functions ─────────────────────────────
def factor_pe(pe_value):
    """Lower PE = cheaper = higher score."""
    if pe_value is None or np.isnan(pe_value) or pe_value <= 0:
        return np.nan
    return -np.log(pe_value)

def factor_pb(pb_value):
    """Lower PB = cheaper = higher score."""
    if pb_value is None or np.isnan(pb_value) or pb_value <= 0:
        return np.nan
    return -np.log(pb_value)

def factor_ps(ps_value):
    """Lower PS = cheaper = higher score."""
    if ps_value is None or np.isnan(ps_value) or ps_value <= 0:
        return np.nan
    return -np.log(ps_value)

# ── Factor Registry ──────────────────────────────────────────
FACTOR_REGISTRY_MR = {
    "rsi":        (factor_rsi_mr,        ["close"]),
    "bollinger":  (factor_bollinger_mr,  ["close"]),
    "momentum":   (factor_momentum,      ["close"]),
    "macd":       (factor_macd,          ["close"]),
    "vol_dev":    (factor_vol_dev,       ["volume"]),
    "low_vol":    (factor_low_vol,       ["close"]),
}

def compute_mr_factors(stock_data, factor_names):
    result = {}
    for name in factor_names:
        fn, cols = FACTOR_REGISTRY_MR[name]
        args = [stock_data[c] for c in cols]
        result[name] = fn(*args)
    return result

def rank_stocks(price_data_dict, fund_data_dict, date_index, weights=None, t=None):
    """Rank stocks by composite score including fundamentals at date t."""
    if weights is None: weights = V43_WEIGHTS
    mr_names = [k for k in weights if k in FACTOR_REGISTRY_MR]
    fund_names = [k for k in weights if k in ("pe", "pb", "ps")]
    
    raw_factors = {}
    for sym, data in price_data_dict.items():
        fv = compute_mr_factors(data, mr_names)
        # Add fundamental factors
        if sym in fund_data_dict and t is not None and date_index is not None and t < len(date_index):
            current_date = date_index[t]
            fund = fund_data_dict[sym]
            # Find the latest fundamental data at or before current_date
            if current_date in fund.index:
                row = fund.loc[current_date]
                fv["pe"] = factor_pe(row.get("peTTM"))
                fv["pb"] = factor_pb(row.get("pbMRQ"))
                fv["ps"] = factor_ps(row.get("psTTM"))
            else:
                before = fund[fund.index <= current_date]
                if len(before) > 0:
                    row = before.iloc[-1]
                    fv["pe"] = factor_pe(row.get("peTTM"))
                    fv["pb"] = factor_pb(row.get("pbMRQ"))
                    fv["ps"] = factor_ps(row.get("psTTM"))
        
        if not all(np.isnan(v) for v in fv.values()):
            raw_factors[sym] = fv
    
    if not raw_factors: return []
    
    # Cross-sectional z-score
    all_names = mr_names + fund_names
    cross_z = {}
    for name in all_names:
        vals = [fv.get(name, np.nan) for fv in raw_factors.values()]
        valid = [v for v in vals if not np.isnan(v)]
        if len(valid) >= 5:
            cross_z[name] = (float(np.mean(valid)), float(np.std(valid, ddof=1)))
    
    ranked = []
    for sym, fv in raw_factors.items():
        cs = composite_score(fv, weights, cross_z)
        if not np.isnan(cs):
            ranked.append((sym, cs))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked


# ── Data Loading ─────────────────────────────────────────────
def load_price_data():
    """Load all stock price data. Returns (stocks, max_len, date_index)."""
    files = sorted(DATA_DIR.glob("*.parquet"))
    stocks = {}
    ml = 0
    date_index = None
    print(f"Loading {len(files)} price files...", flush=True)
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
            }
            ml = max(ml, len(df))
            if date_index is None or len(df.index) > len(date_index):
                date_index = df.index
        except Exception:
            continue
    print(f"  {len(stocks)} stocks, max={ml}, {time.time()-t0:.0f}s", flush=True)
    return stocks, ml, date_index

def load_fundamental_data():
    """Load fundamental PE/PB/PS data."""
    files = sorted(FUND_DIR.glob("*.parquet"))
    fund_data = {}
    if not files:
        print("  No fundamental data found", flush=True)
        return fund_data
    print(f"Loading {len(files)} fundamental files...", flush=True)
    for f in files:
        try:
            df = pd.read_parquet(f)
            sym = f.stem
            # Ensure DatetimeIndex
            if not isinstance(df.index, pd.DatetimeIndex):
                df['date'] = pd.to_datetime(df['date'])
                df = df.set_index('date')
            # Fill missing as NaN
            for col in ['peTTM', 'pbMRQ', 'psTTM']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
                    df.loc[df[col] == 0, col] = np.nan
            fund_data[sym] = df
        except Exception:
            continue
    print(f"  {len(fund_data)} stocks with fundamentals", flush=True)
    return fund_data


# ── Backtest ─────────────────────────────────────────────────
def backtest(stocks, fund_data, date_index, start_idx, end_idx, cfg, warmup=None):
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
                }
            
            active_counts.append(len(snapshot))
            if len(snapshot) < cfg["min_stocks"]:
                positions = {}
                daily_returns.append(0.0)
                continue
            
            # Get date for fundamental data lookup
            current_date = None
            for sym in sorted(stocks.keys()):
                if t < len(stocks[sym]["close"]):
                    # Use the index of the first stock as reference date
                    # (all stocks share the same timeline)
                    break
            
            ranked = rank_stocks(snapshot, fund_data, date_index, V43_WEIGHTS, t=t)
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


def walk_forward(stocks, fund_data, date_index, ml, cfg):
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
        tr = backtest(stocks, fund_data, date_index, 0, tre, cfg)
        ter = backtest(stocks, fund_data, date_index, tre, te, cfg, warmup=cfg["test_warmup_days"])
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
    stocks, ml, date_index = load_price_data()
    fund_data = load_fundamental_data()
    has_fund = len(fund_data) > 0
    
    if not has_fund:
        print("WARNING: No fundamental data. Running V3.5 baseline only.")
        # Use V3.5 weights without fundamental
        global V43_WEIGHTS
        V43_WEIGHTS = {k: v for k, v in V43_WEIGHTS.items() if k in FACTOR_REGISTRY_MR}
        total = sum(V43_WEIGHTS.values())
        V43_WEIGHTS = {k: v/total for k, v in V43_WEIGHTS.items()}
    
    print(f"\nFull bt...", flush=True)
    t1 = time.time()
    full = backtest(stocks, fund_data, date_index, 0, ml, CONFIG)
    print(f"  {time.time()-t1:.0f}s", flush=True)
    
    print(f"\nWF...", flush=True)
    t2 = time.time()
    wf = walk_forward(stocks, fund_data, date_index, ml, CONFIG)
    print(f"  {time.time()-t2:.0f}s", flush=True)
    
    gates = {}
    if full and full["sharpe_ratio"] is not None:
        gates["S"] = full["sharpe_ratio"] >= 1.2
        gates["M"] = full["max_drawdown"] >= -0.15
    else:
        gates["S"] = gates["M"] = False
    gates["D"] = (wf["sharpe_decay"] is not None and wf["sharpe_decay"] <= 0.30)
    gates["A"] = all(gates.values())
    
    suffix = "v4.3_fund" if has_fund else "v4.3_nofund"
    out_path = OUT_DIR / f"wf_backtest_{suffix}_report.json"
    with open(out_path, "w") as f:
        json.dump({
            "ts": datetime.now().isoformat(),
            "version": "V4.3",
            "desc": "V3.5 MR + Fundamental (PE/PB/PS)" if has_fund else "V3.5 MR baseline (no fundamentals)",
            "has_fundamentals": has_fund,
            "n_fund_stocks": len(fund_data),
            "cfg": CONFIG,
            "weights": V43_WEIGHTS,
            "full": full,
            "wf": wf,
            "gates": gates,
            "elapsed": round(time.time() - t0, 1),
        }, f, indent=2, default=str)
    
    print(f"\n{'='*60}")
    desc = f"V4.3 — V3.5 + Fundamental (PE/PB/PS, {len(fund_data)} stocks)" if has_fund else "V4.3 — V3.5 baseline (no fundamentals)"
    print(f"  {desc}")
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
    print(f"  {'✅' if gates['A'] else '❌'} S={'✅' if gates['S'] else '❌'} M={'✅' if gates['M'] else '❌'} D={'✅' if gates['D'] else '❌'}")
    print(f"  ⏱ {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
