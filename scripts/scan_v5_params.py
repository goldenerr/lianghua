#!/usr/bin/env python3.9
"""
V5 — Parameter grid scan for V3.5 MR model.
Tests top_n × rebalance_freq × max_position_pct.
Full backtest only (no WF). Reports Sharpe/MDD/Ret for each combo.
"""
from __future__ import annotations
import json, sys, time
from pathlib import Path
import numpy as np
import pandas as pd

PROJECT = Path("/home/hermes/.hermes/projects/lianghua")
DATA_DIR = PROJECT / "data/parquet"
OUT_DIR = PROJECT / "data/backtest_results"
OUT_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(PROJECT / "src"))
from quant_trading.strategy.factors import composite_score

BASE_CFG = {
    "warmup_days": 252, "min_stocks": 50, "min_history": 252,
    "stamp_duty": 0.0005, "commission": 0.00025,
    "slippage_base": 0.0005, "slippage_factor": 0.10,
    "risk_free_rate": 0.025,
}

V35_WEIGHTS = {"rsi":0.25,"bollinger":0.25,"momentum":0.20,"macd":0.15,"vol_dev":0.10,"low_vol":0.05}

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

MR = {"rsi":(factor_rsi_mr,"close"),"bollinger":(factor_bollinger_mr,"close"),
      "momentum":(factor_momentum,"close"),"macd":(factor_macd,"close"),
      "vol_dev":(factor_vol_dev,"volume"),"low_vol":(factor_low_vol,"close")}

def compute_factors(closes, volumes, factors=None):
    if factors is None: factors=list(V35_WEIGHTS.keys())
    return {n:fn(closes if dt=="close" else volumes) for n,(fn,dt) in MR.items() if n in factors}

def rank_stocks(stock_data, weights=None):
    if weights is None: weights=V35_WEIGHTS
    factors=list(weights.keys())
    raw_factors={}
    for sym,data in stock_data.items():
        fv=compute_factors(np.asarray(data["close"]),np.asarray(data["volume"]),factors)
        if not all(np.isnan(v) for v in fv.values()): raw_factors[sym]=fv
    if not raw_factors: return []
    cross_z={}
    for name in factors:
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
    t0=time.time()
    for i,f in enumerate(files):
        if(i+1)%300==0: print(f"  [{i+1}/{len(files)}] {time.time()-t0:.0f}s",flush=True)
        try:
            df=pd.read_parquet(f)
            if len(df)<BASE_CFG["min_history"]: continue
            ca[f.stem]=df["close"].values.astype(np.float64)
            va[f.stem]=df["volume"].values.astype(np.float64)
            ml=max(ml,len(df))
        except Exception: continue
    print(f"  {len(ca)} stocks, max={ml}, {time.time()-t0:.0f}s",flush=True)
    return ca,va,ml

def backtest(ca, va, start_idx, end_idx, cfg):
    warmup=cfg["warmup_days"]; n_days=end_idx-start_idx
    if n_days<warmup+10: return None
    top_n=cfg["top_n"]; rfreq=cfg["rebalance_freq"]
    valid=[s for s in sorted(ca.keys()) if len(ca[s])>start_idx+warmup]
    if len(valid)<cfg["min_stocks"]: return None
    positions={}; dr=[]; to=[]; ac=[]
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
            ac.append(len(snapshot))
            if len(snapshot)<cfg["min_stocks"]: positions={}; dr.append(0.0); continue
            ranked=rank_stocks(snapshot,V35_WEIGHTS)
            selected=set(sym for sym,_ in ranked[:top_n]); n_sel=len(selected)
            if n_sel==0: positions={}; dr.append(0.0); continue
            w=min(1.0/n_sel,cfg["max_position_pct"])
            if w*n_sel>1.0: w=1.0/n_sel
            base_weights={s:w for s in selected}
            ak=set(positions.keys())|set(base_weights.keys())
            to.append(sum(abs(positions.get(s,0)-base_weights.get(s,0)) for s in ak))
            positions=base_weights
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
    if ann_vol>1e-8: s=(ann_ret-BASE_CFG["risk_free_rate"])/ann_vol
    if s and abs(s)<100: sharpe=round(s,4)
    equity=np.cumprod(1+rets); peak=np.maximum.accumulate(equity)
    max_dd=round(float(np.min((equity-peak)/peak)),4)
    return {"sharpe_ratio":sharpe,"annual_return":round(ann_ret,4),
            "annual_volatility":round(ann_vol,4),"max_drawdown":max_dd,
            "total_return":round(float(equity[-1]-1),4),
            "win_rate":round(float(np.mean(rets>0)),4),
            "avg_active_stocks":round(float(np.mean(ac)),1) if ac else 0,
            "avg_turnover":round(float(np.mean(to)),4) if to else 0,
            "n_rebalances":len(to)}

def main():
    t0=time.time(); ca,va,ml=load()
    print(f"\n{'='*80}")
    print(f"V5 — Parameter Grid Search: top_n × rebalance_freq × max_position_pct")
    print(f"{'='*80}")
    
    top_ns=[15,20,25,30,35]
    rfreqs=[30,45,60,90]
    max_ps=[0.15,0.20,0.25]
    
    results=[]
    for top_n in top_ns:
        for rfreq in rfreqs:
            for mp in max_ps:
                cfg={**BASE_CFG,"top_n":top_n,"rebalance_freq":rfreq,"max_position_pct":mp}
                t1=time.time()
                r=backtest(ca,va,0,ml,cfg)
                el=time.time()-t1
                s=r["sharpe_ratio"] if r else None
                ret=r["annual_return"] if r else None
                dd=r["max_drawdown"] if r else None
                wr=r["win_rate"] if r else None
                at=r.get("avg_active_stocks","?") if r else "?"
                results.append({"top_n":top_n,"rfreq":rfreq,"max_pct":mp,
                                "sharpe":s,"ret":ret,"mdd":dd,"win_rate":wr,
                                "active":at,"elapsed":round(el,1)})
                sym="✅" if s and s>=1.2 and dd and dd>=-0.15 and wr and wr>=0.4 else ("⚠️" if s and s>=1.0 else "❌")
                s_str=f"{s:.4f}" if s else "N/A"
                ret_str=f"{ret:.1%}" if ret is not None else "N/A"
                dd_str=f"{dd:.1%}" if dd is not None else "N/A"
                wr_str=f"{wr:.1%}" if wr is not None else "N/A"
                print(f"  {sym} n={top_n} f={rfreq}d p={mp:.0%} | S={s_str} R={ret_str} MDD={dd_str} WR={wr_str} a={at} ({el:.0f}s)",flush=True)
    
    # Sort by Sharpe descending
    results.sort(key=lambda x: -(x["sharpe"] or -999))
    
    out_path=OUT_DIR/"v5_param_scan_report.json"
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
        wr_str=f"{r['win_rate']:.1%}" if r['win_rate'] is not None else "N/A"
        gates=""
        if r['sharpe'] and r['sharpe']>=1.2: gates+="S"
        if r['mdd'] and r['mdd']>=-0.15: gates+="M"
        if r['win_rate'] and r['win_rate']>=0.4: gates+="W"
        print(f"  #{i+1} n={r['top_n']} f={r['rfreq']}d p={r['max_pct']:.0%} | S={s_str} R={ret_str} MDD={dd_str} WR={wr_str} gates={'✅' if gates=='SMW' else gates if gates else '❌'}")
    
    print(f"\n  ⏱ {time.time()-t0:.0f}s total, {len(results)} combos")

if __name__=="__main__": main()
