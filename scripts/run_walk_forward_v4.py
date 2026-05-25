#!/usr/bin/env python3.9
"""
V4.1 — V3.5 exact + Risk Management overlay only.
NO new factors. Only adds:
  1. Volatility targeting: scale positions to keep portfolio vol at 18%
  2. Cash reserve: hold up to 30% cash when market stress (vol > 25%)
"""
from __future__ import annotations
import json, sys, time
from datetime import datetime
from pathlib import Path
from typing import Optional
import numpy as np
import pandas as pd

from _paths import PROJECT_DIR as PROJECT
from _paths import DATA_DIR
from _paths import RESULTS_DIR as OUT_DIR
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
    # Risk management (only new thing)
    "vol_target": 0.18,         # target 18% annual vol (V3.5 actual was 13.7%)
    "vol_lookback": 60,
    "max_leverage": 1.0,
    "stress_vol_threshold": 0.25,  # when port vol > 25%, start holding cash
    "max_cash": 0.30,              # max 30% cash in stressed markets
}

ALL_CAP_WEIGHTS = {"rsi":0.25,"bollinger":0.25,"momentum":0.20,"macd":0.15,"vol_dev":0.10,"low_vol":0.05}

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

MR={"rsi":(factor_rsi_mr,"close"),"bollinger":(factor_bollinger_mr,"close"),
    "momentum":(factor_momentum,"close"),"macd":(factor_macd,"close"),
    "vol_dev":(factor_vol_dev,"volume"),"low_vol":(factor_low_vol,"close")}

def compute_factors(closes, volumes, factors=None):
    if factors is None: factors=list(MR.keys())
    return {n:fn(closes if dt=="close" else volumes) for n,(fn,dt) in MR.items() if n in factors}

def rank_stocks_mr(stock_data, weights=None):
    if weights is None: weights=ALL_CAP_WEIGHTS
    factors=list(weights.keys())
    raw_factors={}
    for sym,data in stock_data.items():
        fv=compute_factors(np.asarray(data["close"]),np.asarray(data.get("volume",[])),factors)
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
            vc="volume" if "volume" in df.columns else ("vol" if "vol" in df.columns else None)
            va[f.stem]=df[vc].values.astype(np.float64) if vc else np.ones(len(df))
            ml=max(ml,len(df))
        except Exception: continue
    print(f"  {len(ca)} stocks, max={ml}, {time.time()-t0:.0f}s",flush=True)
    return ca,va,ml

# ── Risk Budget Overlay ──────────────────────────────────────
def port_vol_estimate(positions, close_arr, t, lookback=60):
    """Estimate current portfolio volatility."""
    if not positions: return 0.0
    rets_list = []
    for sym, w in positions.items():
        if sym not in close_arr or t < lookback: continue
        p = close_arr[sym][t-lookback+1:t+1]
        if len(p) < 20: continue
        r = np.diff(p) / p[:-1]
        rets_list.append(w * r)
    if not rets_list: return 0.0
    min_len = min(len(r) for r in rets_list)
    if min_len < 10: return 0.0
    port_rets = sum(r[-min_len:] for r in rets_list)
    return float(np.std(port_rets, ddof=1) * np.sqrt(252))

def compute_risk_multiplier(positions, close_arr, t, cfg):
    """Returns (position_multiplier, cash_fraction)."""
    vol = port_vol_estimate(positions, close_arr, t, cfg.get("vol_lookback", 60))
    target = cfg.get("vol_target", 0.18)
    stress_thresh = cfg.get("stress_vol_threshold", 0.25)
    max_cash = cfg.get("max_cash", 0.30)
    
    if vol < 1e-8: return 1.0, 0.0
    
    # Vol targeting: scale to hit target
    scale = target / vol
    scale = min(scale, cfg.get("max_leverage", 1.0))
    scale = max(scale, 0.0)
    
    # Cash reserve: linear ramp from 0% at threshold to max_cash at 2x threshold
    cash = 0.0
    if vol > stress_thresh:
        excess = (vol - stress_thresh) / stress_thresh  # 0.0 at threshold, 1.0 at 2x
        cash = min(excess, 1.0) * max_cash
    
    return scale * (1.0 - cash), cash


def backtest(ca, va, start_idx, end_idx, cfg, warmup=None):
    if warmup is None: warmup=cfg["warmup_days"]
    n_days=end_idx-start_idx
    if n_days<warmup+10: return None
    top_n=min(cfg["top_n"],20); rfreq=cfg["rebalance_freq"]
    valid=[s for s in sorted(ca.keys()) if len(ca[s])>start_idx+warmup]
    if len(valid)<cfg["min_stocks"]: return None
    
    positions:dict[str,float]={}
    daily_returns:list[float]=[]
    turnover_rates:list[float]=[]
    active_counts:list[int]=[]
    cash_fractions:list[float]=[]
    
    for t in range(start_idx+warmup, end_idx-1):
        if (t-start_idx-warmup)%rfreq==0:
            snapshot={}
            for sym in valid:
                prices=ca[sym]
                if t>=len(prices): continue
                lookback=min(252,t+1)
                p_slice=prices[t-lookback+1:t+1]
                if len(p_slice)<60 or np.any(np.isnan(p_slice)): continue
                vols=va.get(sym,np.ones(t+1))
                v_slice=vols[max(0,t-lookback+1):t+1]
                snapshot[sym]={"close":p_slice,"volume":v_slice}
            
            active_counts.append(len(snapshot))
            if len(snapshot)<cfg["min_stocks"]:
                positions={}; daily_returns.append(0.0); cash_fractions.append(0.0); continue
            
            ranked=rank_stocks_mr(snapshot,ALL_CAP_WEIGHTS)
            selected=set(sym for sym,_ in ranked[:top_n])
            n_sel=len(selected)
            if n_sel==0:
                positions={}; daily_returns.append(0.0); cash_fractions.append(0.0); continue
            
            w=min(1.0/n_sel,cfg["max_position_pct"])
            if w*n_sel>1.0: w=1.0/n_sel
            base_weights={s:w for s in selected}
            
            # Risk budget overlay
            mult, cash = compute_risk_multiplier(base_weights, ca, t, cfg)
            target_weights = {s: w * mult for s, w in base_weights.items()}
            cash_fractions.append(cash)
            
            all_keys=set(positions.keys())|set(target_weights.keys())
            turnover=sum(abs(positions.get(s,0)-target_weights.get(s,0)) for s in all_keys)
            turnover_rates.append(turnover)
            slippage=cfg["slippage_base"]*(1+cfg["slippage_factor"]*np.sqrt(turnover))
            positions=target_weights
        
        day_return=0.0
        for sym,w in positions.items():
            if sym not in ca or t+1>=len(ca[sym]): continue
            p_t,p_t1=ca[sym][t],ca[sym][t+1]
            if p_t<=0 or p_t1<=0: continue
            day_return+=w*(p_t1/p_t-1.0)
        # Add risk-free return on cash
        if cash_fractions:
            day_return += cash_fractions[-1] * (cfg["risk_free_rate"]/252)
        daily_returns.append(day_return)
    
    if len(daily_returns)<50: return None
    rets=np.array(daily_returns)
    ann_ret=float(np.mean(rets)*252); ann_vol=float(np.std(rets,ddof=1)*np.sqrt(252))
    sharpe=None
    if ann_vol>1e-8:
        s=(ann_ret-cfg["risk_free_rate"])/ann_vol
        if abs(s)<100: sharpe=round(s,4)
    equity=np.cumprod(1+rets); peak=np.maximum.accumulate(equity)
    max_dd=round(float(np.min((equity-peak)/peak)),4)
    calmar=round(ann_ret/abs(max_dd),4) if abs(max_dd)>1e-10 and ann_vol>1e-8 else 0.0
    
    return {"sharpe_ratio":sharpe,"annual_return":round(ann_ret,4),
            "annual_volatility":round(ann_vol,4),"max_drawdown":max_dd,
            "calmar_ratio":calmar,"win_rate":round(float(np.mean(rets>0)),4),
            "total_return":round(float(equity[-1]-1),4),
            "n_rebalances":len(turnover_rates),"n_days":len(daily_returns),
            "avg_active_stocks":round(float(np.mean(active_counts)),1) if active_counts else 0,
            "avg_turnover":round(float(np.mean(turnover_rates)),4) if turnover_rates else 0,
            "avg_cash":round(float(np.mean(cash_fractions)),4) if cash_fractions else 0}

def walk_forward(ca,va,ml,cfg):
    nf=cfg["n_folds"]; too=int(ml*cfg["oos_pct"]); fs=too//nf
    fts=ml-too; pg=cfg["purge_days"]
    results=[]; is_s=[]; oos_s=[]
    for fold in range(nf):
        ts=fts+fold*fs; te=min(ts+fs,ml); tre=ts-pg
        t0=time.time()
        tr=backtest(ca,va,0,tre,cfg); ter=backtest(ca,va,tre,te,cfg,warmup=cfg["test_warmup_days"])
        el=time.time()-t0
        fr={"fold":fold,"train":tre,"test":te-ts}
        if tr and tr["sharpe_ratio"] is not None: fr["is"]=tr["sharpe_ratio"]; is_s.append(tr["sharpe_ratio"])
        else: fr["is"]=None
        if ter and ter["sharpe_ratio"] is not None: fr["oos"]=ter["sharpe_ratio"]; fr["act"]=ter.get("avg_active_stocks",0); oos_s.append(ter["sharpe_ratio"])
        else: fr["oos"]=None
        results.append(fr)
        print(f"  F{fold}: IS={fr['is']} OOS={fr.get('oos')} ({el:.0f}s) a={fr.get('act','?')}",flush=True)
    ai=float(np.mean(is_s)) if is_s else np.nan; ao=float(np.mean(oos_s)) if oos_s else np.nan
    d=(ai-ao)/abs(ai) if is_s and oos_s and abs(ai)>0.001 else np.nan
    return {"folds":results,"avg_is_sharpe":round(ai,4) if not np.isnan(ai) else None,
            "avg_oos_sharpe":round(ao,4) if not np.isnan(ao) else None,
            "sharpe_decay":round(d,4) if not np.isnan(d) else None}

def main():
    t0=time.time(); ca,va,ml=load()
    print(f"\nFull bt...",flush=True); t1=time.time()
    full=backtest(ca,va,0,ml,CONFIG)
    print(f"  {time.time()-t1:.0f}s",flush=True)
    print(f"\nWF...",flush=True); t2=time.time()
    w=walk_forward(ca,va,ml,CONFIG)
    print(f"  {time.time()-t2:.0f}s",flush=True)
    ov=[r["oos"] for r in w["folds"] if r.get("oos") is not None]
    iv=[r["is"] for r in w["folds"] if r.get("is") is not None]
    g={}
    if full and full["sharpe_ratio"] is not None:
        g["S"]=full["sharpe_ratio"]>=1.2; g["M"]=full["max_drawdown"]>=-0.15
    else: g["S"]=g["M"]=False
    g["D"]=(w["sharpe_decay"] is not None and w["sharpe_decay"]<=0.30)
    g["A"]=all(g.values())
    with open(OUT_DIR/"wf_backtest_v4_report.json","w") as f:
        json.dump({"ts":datetime.now().isoformat(),"cfg":CONFIG,"full":full,"wf":w,
                   "gates":g,"elapsed":round(time.time()-t0,1)},f,indent=2,default=str)
    print(f"\n{'='*60}\n  V4.1 — V3.5 + Risk Budget (vol target + cash)")
    print(f"  {len(ca)} stocks, top-{CONFIG['top_n']}, {CONFIG['rebalance_freq']}d")
    print(f"  Vol target: {CONFIG['vol_target']:.0%}, Cash when >{CONFIG['stress_vol_threshold']:.0%}")
    print(f"{'='*60}")
    if full:
        s=full["sharpe_ratio"]
        print(f"  Full: Sharpe={'N/A' if s is None else f'{s:.4f}'} "
              f"Ret={full['annual_return']:.1%} Vol={full['annual_volatility']:.1%} "
              f"MDD={full['max_drawdown']:.1%}")
        print(f"  Active={full['avg_active_stocks']:.0f} Turnover={full['avg_turnover']:.1%} "
              f"Cash={full.get('avg_cash',0):.1%}")
    for fld in w["folds"]:
        iss=f"{fld['is']:.3f}" if fld.get('is') is not None else "N/A"
        oss=f"{fld.get('oos','N/A')}"
        oss=f"{oss:.3f}" if isinstance(oss,float) else oss
        print(f"  F{fld['fold']}: IS={iss} OOS={oss} a={fld.get('act','?')}")
    print(f"  IS:{w['avg_is_sharpe']} OOS:{w['avg_oos_sharpe']} D:{w['sharpe_decay']}")
    print(f"  {'✅' if g['A'] else '❌'} S={'✅' if g['S'] else '❌'} M={'✅' if g['M'] else '❌'} D={'✅' if g['D'] else '❌'}")
    print(f"  ⏱ {time.time()-t0:.0f}s")

if __name__=="__main__": main()
