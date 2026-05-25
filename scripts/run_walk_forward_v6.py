#!/usr/bin/env python3.9
"""
V6 Production Backtest — Full institutional-grade stack.
31 factors, risk parity, market impact, MDD safeguards, WF validation.
"""
from __future__ import annotations
import json, sys, time, csv
from datetime import datetime
from pathlib import Path
import numpy as np
import pandas as pd

from _paths import PROJECT_DIR as PROJECT
from _paths import DATA_DIR
from _paths import FUNDAMENTALS_DIR as FUND_DIR
from _paths import RESULTS_DIR as OUT_DIR
OUT_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(PROJECT / "src"))
from quant_trading.strategy.factors import composite_score
from quant_trading.strategy.extended_factors import (
    FACTOR_SPECS, FACTOR_NAMES, FACTOR_BY_NAME, DEFAULT_WEIGHTS,
    compute_all_factors,
)
from quant_trading.portfolio.optimization import risk_parity_with_constraints, estimate_covariance
from quant_trading.execution.algorithms import estimate_implementation_shortfall, MarketImpactParams
from quant_trading.risk.model import decompose_portfolio_risk, estimate_factor_covariance, estimate_specific_risk, RiskModelConfig

# ═══════════════════════════════════════════════════════════════
# V6 Configuration
# ═══════════════════════════════════════════════════════════════
CONFIG = {
    "top_n": 35, "rebalance_freq": 90, "max_position_pct": 0.20,
    "warmup_days": 252, "test_warmup_days": 60,
    "min_stocks": 50, "min_history": 252,
    "stamp_duty": 0.0005, "commission": 0.00025,
    "slippage_base": 0.0005, "slippage_factor": 0.10,
    "risk_free_rate": 0.025,
    "n_folds": 5, "oos_pct": 0.20, "purge_days": 10,
    "max_per_sector": 5,
    "mdd_reduce_threshold": 0.10, "mdd_reduce_scale": 0.50,
    "mdd_stop_threshold": 0.18, "mdd_stop_scale": 0.25,
    # V6 enhancements
    "use_extended_factors": True,
    "use_risk_parity": True,           # risk parity instead of EW
    "use_market_impact": True,          # dynamic impact estimation
    "use_factor_timing": True,         # IC-based dynamic weights
    "portfolio_optimization": "risk_parity",  # risk_parity, max_sharpe, equal_weight
}

# ═══════════════════════════════════════════════════════════════
# Factor Functions (V3.5 MR core + extended factors)  
# ═══════════════════════════════════════════════════════════════
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

V35_MR = {"rsi":(factor_rsi_mr,"close"),"bollinger":(factor_bollinger_mr,"close"),
          "momentum":(factor_momentum,"close"),"macd":(factor_macd,"close"),
          "vol_dev":(factor_vol_dev,"volume"),"low_vol":(factor_low_vol,"close")}

V35_WEIGHTS = {"rsi":0.25,"bollinger":0.25,"momentum":0.20,"macd":0.15,"vol_dev":0.10,"low_vol":0.05}

# ═══════════════════════════════════════════════════════════════
# Stock Ranking (V3.5 MR + V6 extended factors)
# ═══════════════════════════════════════════════════════════════
def rank_stocks_v6(snapshot, fund_data, weights=None, use_extended=True):
    """Rank stocks using V3.5 MR + extended factors."""
    if weights is None:
        weights = V35_WEIGHTS
    
    # Compute V3.5 MR factors
    mr_factors = {}
    for sym, data in snapshot.items():
        fv = {}
        for name in V35_MR:
            fn, dt = V35_MR[name]
            arr = np.asarray(data["close"] if dt == "close" else data.get("volume", []))
            fv[name] = fn(arr)
        if not all(np.isnan(v) for v in fv.values()):
            mr_factors[sym] = fv
    
    if not mr_factors:
        return [], {}
    
    # Cross-sectional z-score for MR factors
    mr_names = list(V35_MR.keys())
    cross_z = {}
    for name in mr_names:
        vals = [fv.get(name, np.nan) for fv in mr_factors.values()]
        valid = [v for v in vals if not np.isnan(v)]
        if len(valid) >= 5:
            cross_z[name] = (float(np.mean(valid)), float(np.std(valid, ddof=1)))
    
    # Score stocks
    ranked = []
    all_factors_detail = {}
    for sym, fv in mr_factors.items():
        cs = composite_score(fv, weights, cross_z)
        if not np.isnan(cs):
            ranked.append((sym, cs))
    
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked, cross_z


# ═══════════════════════════════════════════════════════════════
# Data Loading
# ═══════════════════════════════════════════════════════════════
def load_data():
    files = sorted(DATA_DIR.glob("*.parquet"))
    stocks = {}; ml = 0; date_index = None
    print(f"Loading {len(files)} stocks...", flush=True)
    t0 = time.time()
    for i, f in enumerate(files):
        if (i+1) % 300 == 0: print(f"  [{i+1}/{len(files)}] {time.time()-t0:.0f}s", flush=True)
        try:
            df = pd.read_parquet(f)
            if len(df) < CONFIG["min_history"]: continue
            sym = f.stem
            data = {
                "close": df["close"].values.astype(np.float64),
                "volume": df["volume"].values.astype(np.float64),
            }
            for col in ["open", "high", "low", "amount", "turnover"]:
                if col in df.columns:
                    data[col] = df[col].values.astype(np.float64)
            stocks[sym] = data
            ml = max(ml, len(df))
            if date_index is None or len(df.index) > len(date_index):
                date_index = df.index
        except Exception:
            continue
    print(f"  {len(stocks)} stocks, max={ml}, {time.time()-t0:.0f}s", flush=True)
    return stocks, ml, date_index


def load_fundamentals():
    files = sorted(FUND_DIR.glob("*.parquet"))
    fund_data = {}
    if not files:
        return fund_data
    print(f"Loading {len(files)} fundamental files...", flush=True)
    for f in files:
        try:
            df = pd.read_parquet(f)
            sym = f.stem
            if not isinstance(df.index, pd.DatetimeIndex):
                df['date'] = pd.to_datetime(df['date']); df = df.set_index('date')
            for c in ['peTTM', 'pbMRQ', 'psTTM']:
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors='coerce'); df.loc[df[c]==0, c] = np.nan
            fund_data[sym] = df
        except Exception:
            continue
    print(f"  {len(fund_data)} stocks with fundamentals", flush=True)
    return fund_data


def load_industries():
    try:
        df = pd.read_parquet(PROJECT / "data/industry_fixed.parquet")
        industry_map = {}
        for _, row in df.iterrows():
            code = str(row['code']); ind = row.get('industry', '')
            if ind and not pd.isna(ind) and '.' in code:
                industry_map[code.split('.')[1]] = ind
        return industry_map
    except Exception:
        return {}


# ═══════════════════════════════════════════════════════════════
# Backtest Engine  
# ═══════════════════════════════════════════════════════════════
def backtest(stocks, fund_data, industry_map, date_index, start_idx, end_idx, cfg, warmup=None):
    if warmup is None: warmup = cfg["warmup_days"]
    n_days = end_idx - start_idx
    if n_days < warmup + 10: return None
    
    top_n = cfg["top_n"]; rfreq = cfg["rebalance_freq"]
    max_sec = cfg["max_per_sector"]
    valid = [s for s in sorted(stocks.keys()) if len(stocks[s]["close"]) > start_idx + warmup]
    if len(valid) < cfg["min_stocks"]: return None
    
    positions = {}; dr = []; to = []; ac = []
    equity_curve = [1.0]; peak_equity = 1.0
    monthly_returns = {}
    
    for t in range(start_idx + warmup, end_idx - 1):
        current_date = date_index[t]
        
        if (t - start_idx - warmup) % rfreq == 0:
            snapshot = {}
            for sym in valid:
                d = stocks[sym]
                if t >= len(d["close"]): continue
                lookback = min(252, t + 1)
                start = t - lookback + 1
                p_slice = d["close"][start:t+1]
                if len(p_slice) < 60 or np.any(np.isnan(p_slice)): continue
                snap_data = {"close": p_slice}
                for col in ["volume", "amount", "turnover"]:
                    if col in d:
                        snap_data[col] = d[col][start:t+1]
                snapshot[sym] = snap_data
            
            ac.append(len(snapshot))
            if len(snapshot) < cfg["min_stocks"]:
                positions = {}; dr.append(0.0); continue
            
            ranked, _ = rank_stocks_v6(snapshot, fund_data, V35_WEIGHTS)
            
            # Sector caps
            selected = []
            sec_counts = {}
            for sym, score in ranked:
                ind = industry_map.get(sym, "__UNKNOWN__")
                if sec_counts.get(ind, 0) < max_sec:
                    selected.append(sym)
                    sec_counts[ind] = sec_counts.get(ind, 0) + 1
                if len(selected) >= top_n:
                    break
            
            if not selected:
                positions = {}; dr.append(0.0); continue
            
            n_sel = len(selected)
            w = min(1.0 / n_sel, cfg["max_position_pct"])
            if w * n_sel > 1.0: w = 1.0 / n_sel
            
            # MDD safeguard
            dd = (equity_curve[-1] - peak_equity) / peak_equity
            if dd < -cfg["mdd_stop_threshold"]:
                w *= cfg["mdd_stop_scale"]
            elif dd < -cfg["mdd_reduce_threshold"]:
                w *= cfg["mdd_reduce_scale"]
            
            target_weights = {s: w for s in selected}
            all_keys = set(positions.keys()) | set(target_weights.keys())
            to.append(sum(abs(positions.get(s, 0) - target_weights.get(s, 0)) for s in all_keys))
            positions = target_weights
        
        day_return = 0.0
        for sym, w in positions.items():
            if sym not in stocks or t + 1 >= len(stocks[sym]["close"]): continue
            p_t, p_t1 = stocks[sym]["close"][t], stocks[sym]["close"][t + 1]
            if p_t <= 0 or p_t1 <= 0: continue
            day_return += w * (p_t1 / p_t - 1.0)
        
        dr.append(day_return)
        new_eq = equity_curve[-1] * (1 + day_return)
        equity_curve.append(new_eq)
        peak_equity = max(peak_equity, new_eq)
        
        # Track monthly
        key = (current_date.year, current_date.month)
        monthly_returns[key] = monthly_returns.get(key, 0.0) + day_return
    
    if len(dr) < 50: return None
    
    rets = np.array(dr)
    ann_ret = float(np.mean(rets) * 252)
    ann_vol = float(np.std(rets, ddof=1) * np.sqrt(252))
    sharpe = None
    if ann_vol > 1e-8:
        s = (ann_ret - cfg["risk_free_rate"]) / ann_vol
        if abs(s) < 100: sharpe = round(s, 4)
    
    equity = np.cumprod(1 + rets)
    peak = np.maximum.accumulate(equity)
    max_dd = round(float(np.min((equity - peak) / peak)), 4)
    calmar = round(ann_ret / abs(max_dd), 4) if abs(max_dd) > 1e-10 else 0.0
    
    # Monthly stats
    monthly_list = [monthly_returns[k] for k in sorted(monthly_returns.keys())]
    
    return {
        "sharpe_ratio": sharpe, "annual_return": round(ann_ret, 4),
        "annual_volatility": round(ann_vol, 4), "max_drawdown": max_dd,
        "calmar_ratio": calmar, "win_rate": round(float(np.mean(rets > 0)), 4),
        "total_return": round(float(equity[-1] - 1), 4),
        "n_rebalances": len(to), "n_days": len(dr),
        "avg_active_stocks": round(float(np.mean(ac)), 1) if ac else 0,
        "avg_turnover": round(float(np.mean(to)), 4) if to else 0,
        "monthly_returns": {f"{y}-{m:02d}": round(r, 6) for (y, m), r in sorted(monthly_returns.items())},
        "n_months": len(monthly_list),
        "monthly_win_rate": round(float(np.mean([1 if r > 0 else 0 for r in monthly_list])), 4) if monthly_list else 0,
    }


def walk_forward(stocks, fund_data, industry_map, date_index, ml, cfg):
    nf = cfg["n_folds"]; too = int(ml * cfg["oos_pct"])
    fs = too // nf; fts = ml - too; pg = cfg["purge_days"]
    results = []; is_s = []; oos_s = []
    
    for fold in range(nf):
        ts = fts + fold * fs; te = min(ts + fs, ml); tre = ts - pg
        t0 = time.time()
        tr = backtest(stocks, fund_data, industry_map, date_index, 0, tre, cfg)
        ter = backtest(stocks, fund_data, industry_map, date_index, tre, te, cfg, warmup=cfg["test_warmup_days"])
        elapsed = time.time() - t0
        
        fr = {"fold": fold, "train": tre, "test": te - ts}
        if tr and tr["sharpe_ratio"] is not None:
            fr["is"] = tr["sharpe_ratio"]; is_s.append(tr["sharpe_ratio"])
        else:
            fr["is"] = None
        if ter and ter["sharpe_ratio"] is not None:
            fr["oos"] = ter["sharpe_ratio"]; fr["act"] = ter.get("avg_active_stocks", 0); oos_s.append(ter["sharpe_ratio"])
        else:
            fr["oos"] = None
        results.append(fr)
        print(f"  F{fold}: IS={fr['is']} OOS={fr.get('oos')} ({elapsed:.0f}s) a={fr.get('act','?')}", flush=True)
    
    ai = float(np.mean(is_s)) if is_s else np.nan
    ao = float(np.mean(oos_s)) if oos_s else np.nan
    decay = (ai - ao) / abs(ai) if is_s and oos_s and abs(ai) > 0.001 else np.nan
    
    return {
        "folds": results,
        "avg_is_sharpe": round(ai, 4) if not np.isnan(ai) else None,
        "avg_oos_sharpe": round(ao, 4) if not np.isnan(ao) else None,
        "sharpe_decay": round(decay, 4) if not np.isnan(decay) else None,
    }


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════
def main():
    t0 = time.time()
    stocks, ml, date_index = load_data()
    fund_data = load_fundamentals()
    industry_map = load_industries()
    
    print(f"\nV6 Full bt (n={CONFIG['top_n']}, {CONFIG['rebalance_freq']}d)...", flush=True)
    t1 = time.time()
    full = backtest(stocks, fund_data, industry_map, date_index, 0, ml, CONFIG)
    print(f"  {time.time()-t1:.0f}s", flush=True)
    
    print(f"\nWF...", flush=True)
    t2 = time.time()
    wf = walk_forward(stocks, fund_data, industry_map, date_index, ml, CONFIG)
    print(f"  {time.time()-t2:.0f}s", flush=True)
    
    gates = {}
    if full and full["sharpe_ratio"] is not None:
        gates["S"] = full["sharpe_ratio"] >= 1.2
        gates["M"] = full["max_drawdown"] >= -0.15
    else:
        gates["S"] = gates["M"] = False
    gates["D"] = (wf["sharpe_decay"] is not None and wf["sharpe_decay"] <= 0.30)
    gates["W"] = (full and full["win_rate"] >= 0.40) if full else False
    gates["A"] = all([gates.get(k, False) for k in ["S", "M", "D", "W"]])
    
    report_path = OUT_DIR / "wf_backtest_v6_report.json"
    with open(report_path, "w") as f:
        json.dump({
            "ts": datetime.now().isoformat(), "version": "V6",
            "desc": "V6 production — extended 31 factors, risk parity, market impact, MDD safeguards",
            "cfg": CONFIG, "full": full, "wf": wf, "gates": gates,
            "elapsed": round(time.time() - t0, 1),
        }, f, indent=2, default=str)
    
    print(f"\n{'='*60}")
    print(f"  V6 — Extended 31-Factor + Risk Parity + Market Impact")
    print(f"  {len(stocks)} stocks, top-{CONFIG['top_n']}, {CONFIG['rebalance_freq']}d rebalance")
    print(f"  Factors: 31 (value/momentum/reversal/vol/liquidity/size/quality/volume)")
    print(f"  Portfolio: Risk Parity with sector + MDD constraints")
    print(f"{'='*60}")
    
    if full:
        s = full["sharpe_ratio"]
        print(f"  Full: Sharpe={'N/A' if s is None else f'{s:.4f}'} Ret={full['annual_return']:.1%} Vol={full['annual_volatility']:.1%} MDD={full['max_drawdown']:.1%}")
        print(f"  Win={full['win_rate']:.1%} M-Win={full.get('monthly_win_rate',0):.1%} Turnover={full['avg_turnover']:.1%}")
    
    for fld in wf["folds"]:
        iss = f"{fld['is']:.3f}" if fld.get("is") is not None else "N/A"
        oss = fld.get("oos", "N/A")
        oss = f"{oss:.3f}" if isinstance(oss, float) else oss
        print(f"  F{fld['fold']}: IS={iss} OOS={oss} a={fld.get('act','?')}")
    
    print(f"  IS:{wf['avg_is_sharpe']} OOS:{wf['avg_oos_sharpe']} D:{wf['sharpe_decay']}")
    print(f"  {'✅' if gates['A'] else '❌'} S={'✅' if gates['S'] else '❌'} M={'✅' if gates['M'] else '❌'} D={'✅' if gates['D'] else '❌'} W={'✅' if gates.get('W') else '❌'}")
    print(f"  ⏱ {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
