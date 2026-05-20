#!/usr/bin/env python3.9
"""
V5.4 — V5.2 base + Sector Diversification Constraint.
Max 5 stocks per industry (14% of 35). Skips stocks exceeding cap.
"""
from __future__ import annotations
import json, sys, time
from datetime import datetime
from pathlib import Path
import numpy as np
import pandas as pd

PROJECT = Path("/home/hermes/.hermes/projects/lianghua")
DATA_DIR = PROJECT / "data/parquet"
OUT_DIR = PROJECT / "data/backtest_results"
OUT_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(PROJECT / "src"))
from quant_trading.strategy.factors import composite_score

CONFIG = {
    "top_n": 35, "rebalance_freq": 90, "max_position_pct": 0.20,
    "warmup_days": 252, "test_warmup_days": 60,
    "min_stocks": 50, "min_history": 252,
    "stamp_duty": 0.0005, "commission": 0.00025,
    "slippage_base": 0.0005, "slippage_factor": 0.10,
    "risk_free_rate": 0.025,
    "n_folds": 5, "oos_pct": 0.20, "purge_days": 10,
    # Sector constraints
    "max_per_sector": 5,
    # Production MDD safeguards (AGENTS.md §3)
    "mdd_reduce_threshold": 0.15,   # reduce to 50% at 15% DD
    "mdd_reduce_scale": 0.50,
    "mdd_stop_threshold": 0.25,     # stop all trading at 25% DD
    "mdd_stop_scale": 0.0,
}

V35_WEIGHTS = {"rsi":0.25,"bollinger":0.25,"momentum":0.20,"macd":0.15,"vol_dev":0.10,"low_vol":0.05}

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
    print(f"Loading {len(files)} stocks...",flush=True); t0=time.time()
    for i,f in enumerate(files):
        if(i+1)%300==0: print(f"  [{i+1}/{len(files)}] {time.time()-t0:.0f}s",flush=True)
        try:
            df=pd.read_parquet(f)
            if len(df)<CONFIG["min_history"]: continue
            ca[f.stem]=df["close"].values.astype(np.float64)
            va[f.stem]=df["volume"].values.astype(np.float64)
            ml=max(ml,len(df))
        except Exception: continue
    print(f"  {len(ca)} stocks, max={ml}, {time.time()-t0:.0f}s",flush=True)
    return ca,va,ml

def load_industries():
    """Load industry mapping: code → industry name."""
    try:
        df = pd.read_parquet(PROJECT / "data/industry_fixed.parquet")
    except Exception:
        return {}
    
    industry_map = {}
    for _, row in df.iterrows():
        code = row['code']  # e.g., "sh.600000"
        ind = row.get('industry', '')
        if not ind or pd.isna(ind):
            continue
        # Extract numeric code from "sh.600000" → "600000"
        if '.' in str(code):
            numeric = str(code).split('.')[1]
        else:
            numeric = str(code)
        industry_map[numeric] = ind
    
    print(f"  {len(industry_map)} stocks with industry data", flush=True)
    return industry_map

def select_with_sector_caps(ranked, industry_map, top_n, max_per_sector):
    """Select top_n stocks respecting sector caps."""
    selected = []
    sector_counts = {}
    
    for sym, score in ranked:
        industry = industry_map.get(sym, '__UNKNOWN__')
        count = sector_counts.get(industry, 0)
        if count < max_per_sector:
            selected.append((sym, score))
            sector_counts[industry] = count + 1
        if len(selected) >= top_n:
            break
    
    return selected

def backtest(ca, va, industry_map, start_idx, end_idx, cfg, warmup=None):
    if warmup is None: warmup=cfg["warmup_days"]
    n_days=end_idx-start_idx
    if n_days<warmup+10: return None
    top_n=cfg["top_n"]; rfreq=cfg["rebalance_freq"]; max_sec=cfg["max_per_sector"]
    valid=[s for s in sorted(ca.keys()) if len(ca[s])>start_idx+warmup]
    if len(valid)<cfg["min_stocks"]: return None
    
    positions={}; dr=[]; to=[]; ac=[]
    equity_curve = [1.0]  # track portfolio equity for DD calculation
    peak_equity = 1.0
    
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
            selected=select_with_sector_caps(ranked, industry_map, top_n, max_sec)
            selected_syms=set(sym for sym,_ in selected)
            n_sel=len(selected_syms)
            if n_sel==0: positions={}; dr.append(0.0); continue
            w=min(1.0/n_sel,cfg["max_position_pct"])
            if w*n_sel>1.0: w=1.0/n_sel
            base_weights={s:w for s in selected_syms}
            ak=set(positions.keys())|set(base_weights.keys())
            to.append(sum(abs(positions.get(s,0)-base_weights.get(s,0)) for s in ak))
            sl=cfg["slippage_base"]*(1+cfg["slippage_factor"]*np.sqrt(to[-1]))
            positions=base_weights
        
        day_return=0.0
        for sym,w in positions.items():
            if sym not in ca or t+1>=len(ca[sym]): continue
            p_t,p_t1=ca[sym][t],ca[sym][t+1]
            if p_t<=0 or p_t1<=0: continue
            day_return+=w*(p_t1/p_t-1.0)
        dr.append(day_return)
        
        # Update equity curve for MDD tracking
        new_equity = equity_curve[-1] * (1 + day_return)
        equity_curve.append(new_equity)
        peak_equity = max(peak_equity, new_equity)
        current_dd = (new_equity - peak_equity) / peak_equity
        
        # Production MDD safeguards (AGENTS.md §3)
        if cfg.get("mdd_stop_threshold") and current_dd < -cfg["mdd_stop_threshold"]:
            positions = {}  # Stop all trading
        elif cfg.get("mdd_reduce_threshold") and current_dd < -cfg["mdd_reduce_threshold"]:
            # Scale down existing positions
            scale = cfg.get("mdd_reduce_scale", 0.5)
            positions = {s: w * scale for s, w in positions.items()}
    
    if len(dr)<50: return None
    rets=np.array(dr); ann_ret=float(np.mean(rets)*252); ann_vol=float(np.std(rets,ddof=1)*np.sqrt(252))
    sharpe=None
    if ann_vol>1e-8:
        s=(ann_ret-cfg["risk_free_rate"])/ann_vol
        if abs(s)<100: sharpe=round(s,4)
    equity=np.cumprod(1+rets); peak=np.maximum.accumulate(equity)
    max_dd=round(float(np.min((equity-peak)/peak)),4)
    calmar=round(ann_ret/abs(max_dd),4) if abs(max_dd)>1e-10 else 0.0
    return {"sharpe_ratio":sharpe,"annual_return":round(ann_ret,4),
            "annual_volatility":round(ann_vol,4),"max_drawdown":max_dd,
            "calmar_ratio":calmar,"win_rate":round(float(np.mean(rets>0)),4),
            "total_return":round(float(equity[-1]-1),4),
            "n_rebalances":len(to),"n_days":len(dr),
            "avg_active_stocks":round(float(np.mean(ac)),1) if ac else 0,
            "avg_turnover":round(float(np.mean(to)),4) if to else 0}

def walk_forward(ca,va,industry_map,ml,cfg):
    nf=cfg["n_folds"]; too=int(ml*cfg["oos_pct"]); fs=too//nf
    fts=ml-too; pg=cfg["purge_days"]
    results=[]; is_s=[]; oos_s=[]
    for fold in range(nf):
        ts=fts+fold*fs; te=min(ts+fs,ml); tre=ts-pg
        t0=time.time()
        tr=backtest(ca,va,industry_map,0,tre,cfg); ter=backtest(ca,va,industry_map,tre,te,cfg,warmup=cfg["test_warmup_days"])
        el=time.time()-t0
        fr={"fold":fold,"train":tre,"test":te-ts}
        if tr and tr["sharpe_ratio"] is not None: fr["is"]=tr["sharpe_ratio"]; is_s.append(tr["sharpe_ratio"])
        else: fr["is"]=None
        if ter and ter["sharpe_ratio"] is not None: fr["oos"]=ter["sharpe_ratio"]; fr["act"]=ter.get("avg_active_stocks",0); oos_s.append(ter["sharpe_ratio"])
        else: fr["oos"]=None
        results.append(fr); print(f"  F{fold}: IS={fr['is']} OOS={fr.get('oos')} ({el:.0f}s) a={fr.get('act','?')}",flush=True)
    ai=float(np.mean(is_s)) if is_s else np.nan; ao=float(np.mean(oos_s)) if oos_s else np.nan
    d=(ai-ao)/abs(ai) if is_s and oos_s and abs(ai)>0.001 else np.nan
    return {"folds":results,"avg_is_sharpe":round(ai,4) if not np.isnan(ai) else None,
            "avg_oos_sharpe":round(ao,4) if not np.isnan(ao) else None,
            "sharpe_decay":round(d,4) if not np.isnan(d) else None}

def main():
    t0=time.time(); ca,va,ml=load(); im=load_industries()
    print(f"\nFull bt (n=35, 90d, sector cap={CONFIG['max_per_sector']})...",flush=True); t1=time.time()
    full=backtest(ca,va,im,0,ml,CONFIG)
    print(f"  {time.time()-t1:.0f}s",flush=True)
    print(f"\nWF...",flush=True); t2=time.time()
    w=walk_forward(ca,va,im,ml,CONFIG)
    print(f"  {time.time()-t2:.0f}s",flush=True)
    g={}
    if full and full["sharpe_ratio"] is not None: g["S"]=full["sharpe_ratio"]>=1.2; g["M"]=full["max_drawdown"]>=-0.15
    else: g["S"]=g["M"]=False
    g["D"]=(w["sharpe_decay"] is not None and w["sharpe_decay"]<=0.30); g["W"]=(full and full["win_rate"]>=0.40)
    g["A"]=all([g.get(k,False) for k in ["S","M","D","W"]])
    with open(OUT_DIR/"wf_backtest_v5.8_report.json","w") as f:
        json.dump({"ts":datetime.now().isoformat(),"version":"V5.8",
                   "desc":f"n=35 90d + sector cap (max {CONFIG['max_per_sector']} per industry)",
                   "cfg":CONFIG,"full":full,"wf":w,"gates":g,"elapsed":round(time.time()-t0,1)},f,indent=2,default=str)
    print(f"\n{'='*60}\n  V5.8 — n=35, 90d + Sector Cap (max {CONFIG['max_per_sector']}/industry)")
    print(f"  {len(ca)} stocks, {len(im)} with industries, top-{CONFIG['top_n']}, {CONFIG['rebalance_freq']}d\n{'='*60}")
    if full:
        s=full["sharpe_ratio"]; print(f"  Full: Sharpe={'N/A' if s is None else f'{s:.4f}'} Ret={full['annual_return']:.1%} Vol={full['annual_volatility']:.1%} MDD={full['max_drawdown']:.1%}")
        print(f"  Win={full['win_rate']:.1%} Turnover={full['avg_turnover']:.1%}")
    for fld in w["folds"]:
        iss=f"{fld['is']:.3f}" if fld.get('is') is not None else "N/A"
        oss=fld.get('oos','N/A'); oss=f"{oss:.3f}" if isinstance(oss,float) else oss
        print(f"  F{fld['fold']}: IS={iss} OOS={oss} a={fld.get('act','?')}")
    print(f"  IS:{w['avg_is_sharpe']} OOS:{w['avg_oos_sharpe']} D:{w['sharpe_decay']}")
    print(f"  {'✅' if g['A'] else '❌'} S={'✅' if g['S'] else '❌'} M={'✅' if g['M'] else '❌'} D={'✅' if g['D'] else '❌'} W={'✅' if g.get('W') else '❌'}")
    print(f"  ⏱ {time.time()-t0:.0f}s")

if __name__=="__main__": main()
