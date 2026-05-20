#!/usr/bin/env python3.9
"""
V6 — MR Factor Weight Grid Search.
Scans weight combinations for the 6 V3.5 MR factors.
Uses best params from V5 scan (top_n/rebalance/max_pct).
"""
from __future__ import annotations
import json, sys, time, itertools
from pathlib import Path
import numpy as np
import pandas as pd

PROJECT = Path("/home/hermes/.hermes/projects/lianghua")
DATA_DIR = PROJECT / "data/parquet"
OUT_DIR = PROJECT / "data/backtest_results"
OUT_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(PROJECT / "src"))
from quant_trading.strategy.factors import composite_score

# Best from V3.5 baseline or V5 scan
BEST_CFG = {"top_n":30,"rebalance_freq":60,"max_position_pct":0.20}
BASE_CFG = {"warmup_days":252,"min_stocks":50,"min_history":252,
            "stamp_duty":0.0005,"commission":0.00025,
            "slippage_base":0.0005,"slippage_factor":0.10,"risk_free_rate":0.025}

# ── Factor Functions ─────────────────────────────────────────
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
    if len(s)<span: return np.nan; a=2.0/(span+1); r=s[:span].mean()
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

def compute_factors(closes, volumes, factors=None):
    names = list(MR.keys()) if factors is None else factors
    return {n:fn(closes if dt=="close" else volumes) for n,(fn,dt) in MR.items() if n in names}

def rank_stocks(stock_data, weights):
    factor_names = list(weights.keys())
    raw_factors = {}
    for sym,data in stock_data.items():
        fv = compute_factors(np.asarray(data["close"]),np.asarray(data["volume"]),factor_names)
        if not all(np.isnan(v) for v in fv.values()): raw_factors[sym]=fv
    if not raw_factors: return []
    cross_z={}
    for name in factor_names:
        vals=[fv.get(name,np.nan) for fv in raw_factors.values()]
        valid=[v for v in vals if not np.isnan(v)]
        if len(valid)>=5: cross_z[name]=(float(np.mean(valid)),float(np.std(valid,ddof=1)))
    ranked=[]
    for sym,fv in raw_factors.items():
        cs=composite_score(fv,weights,cross_z)
        if not np.isnan(cs): ranked.append((sym,cs))
    ranked.sort(key=lambda x:x[1],reverse=True)
    return ranked

def load():
    files=sorted(DATA_DIR.glob("*.parquet")); ca={}; va={}; ml=0
    for f in files:
        try:
            df=pd.read_parquet(f)
            if len(df)<BASE_CFG["min_history"]: continue
            ca[f.stem]=df["close"].values.astype(np.float64)
            va[f.stem]=df["volume"].values.astype(np.float64)
            ml=max(ml,len(df))
        except: continue
    return ca,va,ml

def backtest(ca, va, weights, start_idx, end_idx, params):
    warmup=BASE_CFG["warmup_days"]; n_days=end_idx-start_idx
    if n_days<warmup+10: return None
    top_n=params["top_n"]; rfreq=params["rebalance_freq"]
    valid=[s for s in sorted(ca.keys()) if len(ca[s])>start_idx+warmup]
    if len(valid)<BASE_CFG["min_stocks"]: return None
    positions={}; dr=[]
    for t in range(start_idx+warmup, end_idx-1):
        if (t-start_idx-warmup)%rfreq==0:
            snapshot={}
            for sym in valid:
                prices=ca[sym]
                if t>=len(prices): continue
                lookback=min(252,t+1); p_slice=prices[t-lookback+1:t+1]
                if len(p_slice)<60 or np.any(np.isnan(p_slice)): continue
                vols=va.get(sym,np.ones(t+1)); v_slice=vols[max(0,t-lookback+1):t+1]
                snapshot[sym]={"close":p_slice,"volume":v_slice}
            if len(snapshot)<BASE_CFG["min_stocks"]: positions={}; dr.append(0.0); continue
            ranked=rank_stocks(snapshot, weights)
            selected=set(sym for sym,_ in ranked[:top_n]); n_sel=len(selected)
            if n_sel==0: positions={}; dr.append(0.0); continue
            w=min(1.0/n_sel,params["max_position_pct"])
            if w*n_sel>1.0: w=1.0/n_sel
            positions={s:w for s in selected}
        day_return=0.0
        for sym,w in positions.items():
            if sym not in ca or t+1>=len(ca[sym]): continue
            p_t,p_t1=ca[sym][t],ca[sym][t+1]
            if p_t<=0 or p_t1<=0: continue
            day_return+=w*(p_t1/p_t-1.0)
        dr.append(day_return)
    if len(dr)<50: return None
    rets=np.array(dr); ann_ret=float(np.mean(rets)*252); ann_vol=float(np.std(rets,ddof=1)*np.sqrt(252))
    sharpe=None
    if ann_vol>1e-8:
        s=(ann_ret-BASE_CFG["risk_free_rate"])/ann_vol
        if abs(s)<100: sharpe=round(s,4)
    equity=np.cumprod(1+rets); peak=np.maximum.accumulate(equity)
    max_dd=round(float(np.min((equity-peak)/peak)),4)
    return {"sharpe_ratio":sharpe,"annual_return":round(ann_ret,4),
            "max_drawdown":max_dd,"win_rate":round(float(np.mean(rets>0)),4)}

# Generate weight combos: each factor gets 5/15/25/35%, normalized
def gen_weight_combos():
    """Generate sparse weight combos for 6 factors.
    Each factor can take 0, 5, 10, 15, 20, 25, 30, 35% (multiples of 5).
    Normalize to sum=1.0. Skip trivial combos (only 1-2 factors)."""
    vals = [0, 5, 10, 15, 20, 25, 30, 35]
    names = ["rsi","bollinger","momentum","macd","vol_dev","low_vol"]
    # Sample ~200 combos: systematic variation
    combos = []
    # Base: vary dominant pair
    for a in [15,20,25,30,35]:
        for b in [15,20,25,30,35]:
            if a+b>70: continue
            # Distribute remaining across other 4
            rem = 100 - a - b
            if rem < 20: continue
            # Even split remaining
            base = rem // 4
            r1 = base + (rem % 4 > 0)
            r2 = base + (rem % 4 > 1)
            r3 = base + (rem % 4 > 2)
            r4 = base
            w = {names[0]:a/100, names[1]:b/100, names[2]:r1/100, 
                 names[3]:r2/100, names[4]:r3/100, names[5]:r4/100}
            # Only include if >=3 factors have weight > 0.05
            nz = sum(1 for v in w.values() if v > 0.05)
            if nz >= 3: combos.append(w)
    
    # Vary which pair is dominant
    for dom_a, dom_b in [(0,2),(0,3),(1,2),(1,3),(2,3),(2,4)]:
        for a in [20,25,30,35]:
            for b in [15,20,25,30]:
                if a+b > 75: continue
                rem = 100 - a - b
                if rem < 15: continue
                other_weights = [rem/4]*4
                w = {n:0.0 for n in names}
                w[names[dom_a]] = a/100
                w[names[dom_b]] = b/100
                other_idx = [i for i in range(6) if i not in (dom_a,dom_b)]
                for j,oi in enumerate(other_idx):
                    w[names[oi]] = other_weights[j]
                nz = sum(1 for v in w.values() if v > 0.05)
                if nz >= 3: combos.append(w)
    
    # Equal weight as control
    combos.append({n:1/6 for n in names})
    
    # V3.5 baseline
    combos.append({"rsi":0.25,"bollinger":0.25,"momentum":0.20,"macd":0.15,"vol_dev":0.10,"low_vol":0.05})
    
    return combos

def main():
    t0=time.time()
    ca,va,ml=load()
    print(f"Loaded {len(ca)} stocks, max={ml}, {time.time()-t0:.0f}s",flush=True)
    
    combos=gen_weight_combos()
    print(f"\n{'='*80}")
    print(f"V6 — Factor Weight Grid Search ({len(combos)} combos)")
    print(f"{'='*80}")
    
    results=[]
    for i,w in enumerate(combos):
        t1=time.time()
        r=backtest(ca,va,w,0,ml,BEST_CFG)
        el=time.time()-t1
        s=r["sharpe_ratio"] if r else None
        ret=r["annual_return"] if r else None
        dd=r["max_drawdown"] if r else None
        results.append({"weights":{k:round(v,4) for k,v in w.items()},"sharpe":s,"ret":ret,"mdd":dd,"elapsed":el})
        s_str=f"{s:.4f}" if s else "N/A"
        ret_str=f"{ret:.1%}" if ret is not None else "N/A"
        dd_str=f"{dd:.1%}" if dd is not None else "N/A"
        w_str=" ".join(f"{k}={v:.0%}" for k,v in w.items())
        print(f"  [{i+1}/{len(combos)}] S={s_str} R={ret_str} MDD={dd_str} | {w_str} ({el:.0f}s)",flush=True)
    
    results.sort(key=lambda x:-(x["sharpe"] or -999))
    
    out_path=OUT_DIR/"v6_weight_scan_report.json"
    with open(out_path,"w") as f:
        json.dump({"ts":str(pd.Timestamp.now()),"n_combos":len(results),
                   "best":results[:5],"all":results},f,indent=2,default=str)
    
    print(f"\n{'='*80}")
    print(f"TOP 5 — by Sharpe")
    print(f"{'='*80}")
    for i,r in enumerate(results[:5]):
        s_str=f"{r['sharpe']:.4f}" if r['sharpe'] else "N/A"
        ret_str=f"{r['ret']:.1%}" if r['ret'] is not None else "N/A"
        dd_str=f"{r['mdd']:.1%}" if r['mdd'] is not None else "N/A"
        w_str=" ".join(f"{k}={v:.0%}" for k,v in r['weights'].items())
        print(f"  #{i+1} S={s_str} R={ret_str} MDD={dd_str} | {w_str}")
    
    print(f"\n  ⏱ {time.time()-t0:.0f}s total")

if __name__=="__main__": main()
