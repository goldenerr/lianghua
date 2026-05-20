#!/usr/bin/env python3.9
"""
V4.4 — V3.5 EXACT MR weights + Fundamentals as additive overlay.
MR factors keep their original relative proportions (0.25/0.25/0.20/...).
Fundamentals added at 10% total, MR scaled to 90%.
Tests whether PE/PB/PS can ADD alpha without diluting the MR core.
"""
from __future__ import annotations
import json, sys, time
from datetime import datetime
from pathlib import Path
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

# ── V3.5 EXACT ───────────────────────────────────────────────
V35_MR = {"rsi":0.25,"bollinger":0.25,"momentum":0.20,"macd":0.15,"vol_dev":0.10,"low_vol":0.05}

def make_weights(fund_weight=0.10):
    """Scale V3.5 MR to (1-fund_weight), add fundamentals at fund_weight."""
    mr_scale = 1.0 - fund_weight
    w = {k: v * mr_scale for k, v in V35_MR.items()}
    w["pe"] = fund_weight * 0.50
    w["pb"] = fund_weight * 0.30
    w["ps"] = fund_weight * 0.20
    return w

V44_WEIGHTS = make_weights(0.10)

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
    a=2.0/(span+1)
    r=s[:span].mean()
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

MR_REGISTRY = {
    "rsi":(factor_rsi_mr,["close"]),"bollinger":(factor_bollinger_mr,["close"]),
    "momentum":(factor_momentum,["close"]),"macd":(factor_macd,["close"]),
    "vol_dev":(factor_vol_dev,["volume"]),"low_vol":(factor_low_vol,["close"]),
}

def _fund(s, col): v=s.get(col); return np.nan if v is None or np.isnan(v) or v<=0 else -np.log(v)

def rank_stocks(price_data, fund_data, date_index, weights=None, t=None):
    if weights is None: weights = V44_WEIGHTS
    mr_names = [k for k in weights if k in MR_REGISTRY]
    fund_names = [k for k in weights if k in ("pe","pb","ps")]
    raw = {}
    for sym,data in price_data.items():
        fv = {}
        for name in mr_names:
            fn,cols = MR_REGISTRY[name]
            fv[name] = fn(*[data[c] for c in cols])
        if sym in fund_data and t is not None and date_index is not None and t < len(date_index):
            cd = date_index[t]; fd = fund_data[sym]
            row = fd.loc[cd] if cd in fd.index else (fd[fd.index <= cd].iloc[-1] if len(fd[fd.index <= cd])>0 else None)
            if row is not None:
                fv["pe"]=_fund(row,"peTTM"); fv["pb"]=_fund(row,"pbMRQ"); fv["ps"]=_fund(row,"psTTM")
        if not all(np.isnan(v) for v in fv.values()): raw[sym]=fv
    if not raw: return []
    cz={}
    for name in mr_names+fund_names:
        vals=[fv.get(name,np.nan) for fv in raw.values()]; valid=[v for v in vals if not np.isnan(v)]
        if len(valid)>=5: cz[name]=(float(np.mean(valid)),float(np.std(valid,ddof=1)))
    ranked=[(sym,composite_score(fv,weights,cz)) for sym,fv in raw.items()]
    ranked=[(s,c) for s,c in ranked if not np.isnan(c)]; ranked.sort(key=lambda x:x[1],reverse=True)
    return ranked

def load_price():
    files=sorted(DATA_DIR.glob("*.parquet")); st={}; ml=0; di=None
    print(f"Loading {len(files)} price files...",flush=True); t0=time.time()
    for i,f in enumerate(files):
        if(i+1)%300==0:print(f"  [{i+1}/{len(files)}] {time.time()-t0:.0f}s",flush=True)
        try:
            df=pd.read_parquet(f)
            if len(df)<CONFIG["min_history"]: continue
            st[f.stem]={"close":df["close"].values.astype(np.float64),"volume":df["volume"].values.astype(np.float64)}
            ml=max(ml,len(df))
            if di is None or len(df.index)>len(di): di=df.index
        except: continue
    print(f"  {len(st)} stocks, max={ml}, {time.time()-t0:.0f}s",flush=True)
    return st,ml,di

def load_fund():
    files=sorted(FUND_DIR.glob("*.parquet")); fd={}
    if not files: return fd
    print(f"Loading {len(files)} fundamental files...",flush=True)
    for f in files:
        try:
            df=pd.read_parquet(f); sym=f.stem
            if not isinstance(df.index,pd.DatetimeIndex): df['date']=pd.to_datetime(df['date']); df=df.set_index('date')
            for c in ['peTTM','pbMRQ','psTTM']:
                if c in df.columns: df[c]=pd.to_numeric(df[c],errors='coerce'); df.loc[df[c]==0,c]=np.nan
            fd[sym]=df
        except: continue
    print(f"  {len(fd)} stocks with fundamentals",flush=True)
    return fd

def backtest(st,fd,di,si,ei,cfg,warmup=None):
    if warmup is None: warmup=cfg["warmup_days"]
    nd=ei-si
    if nd<warmup+10: return None
    tn=min(cfg["top_n"],20); rf=cfg["rebalance_freq"]
    vl=[s for s in sorted(st.keys()) if len(st[s]["close"])>si+warmup]
    if len(vl)<cfg["min_stocks"]: return None
    pos={}; dr=[]; to=[]; ac=[]
    for t in range(si+warmup,ei-1):
        if (t-si-warmup)%rf==0:
            snap={}
            for sym in vl:
                d=st[sym]
                if t>=len(d["close"]): continue
                lb=min(252,t+1); stt=t-lb+1; ps=d["close"][stt:t+1]
                if len(ps)<60 or np.any(np.isnan(ps)): continue
                snap[sym]={"close":ps,"volume":d["volume"][stt:t+1]}
            ac.append(len(snap))
            if len(snap)<cfg["min_stocks"]: pos={}; dr.append(0.0); continue
            ranked=rank_stocks(snap,fd,di,V44_WEIGHTS,t=t)
            sel=set(s for s,_ in ranked[:tn]); ns=len(sel)
            if ns==0: pos={}; dr.append(0.0); continue
            w=min(1.0/ns,cfg["max_position_pct"])
            if w*ns>1.0: w=1.0/ns
            tw={s:w for s in sel}
            ak=set(pos.keys())|set(tw.keys())
            to.append(sum(abs(pos.get(s,0)-tw.get(s,0)) for s in ak))
            pos=tw
        dr_t=0.0
        for sym,w in pos.items():
            if sym not in st or t+1>=len(st[sym]["close"]): continue
            p0,p1=st[sym]["close"][t],st[sym]["close"][t+1]
            if p0<=0 or p1<=0: continue
            dr_t+=w*(p1/p0-1.0)
        dr.append(dr_t)
    if len(dr)<50: return None
    rets=np.array(dr); ar=float(np.mean(rets)*252); av=float(np.std(rets,ddof=1)*np.sqrt(252))
    sh=None
    if av>1e-8: s=(ar-cfg["risk_free_rate"])/av
    if s and abs(s)<100: sh=round(s,4)
    eq=np.cumprod(1+rets); pk=np.maximum.accumulate(eq); md=round(float(np.min((eq-pk)/pk)),4)
    cm=round(ar/abs(md),4) if abs(md)>1e-10 else 0.0
    return {"sharpe_ratio":sh,"annual_return":round(ar,4),"annual_volatility":round(av,4),
            "max_drawdown":md,"calmar_ratio":cm,"win_rate":round(float(np.mean(rets>0)),4),
            "total_return":round(float(eq[-1]-1),4),"n_rebalances":len(to),"n_days":len(dr),
            "avg_active_stocks":round(float(np.mean(ac)),1) if ac else 0,
            "avg_turnover":round(float(np.mean(to)),4) if to else 0}

def wf(st,fd,di,ml,cfg):
    nf=cfg["n_folds"]; too=int(ml*cfg["oos_pct"]); fs=too//nf; fts=ml-too; pg=cfg["purge_days"]
    res=[]; iss=[]; oos=[]
    for fold in range(nf):
        ts=fts+fold*fs; te=min(ts+fs,ml); tre=ts-pg; t0=time.time()
        tr=backtest(st,fd,di,0,tre,cfg); ter=backtest(st,fd,di,tre,te,cfg,warmup=cfg["test_warmup_days"])
        el=time.time()-t0; fr={"fold":fold,"train":tre,"test":te-ts}
        if tr and tr["sharpe_ratio"] is not None: fr["is"]=tr["sharpe_ratio"]; iss.append(tr["sharpe_ratio"])
        else: fr["is"]=None
        if ter and ter["sharpe_ratio"] is not None: fr["oos"]=ter["sharpe_ratio"]; fr["act"]=ter.get("avg_active_stocks",0); oos.append(ter["sharpe_ratio"])
        else: fr["oos"]=None
        res.append(fr); print(f"  F{fold}: IS={fr['is']} OOS={fr.get('oos')} ({el:.0f}s) a={fr.get('act','?')}",flush=True)
    ai=float(np.mean(iss)) if iss else np.nan; ao=float(np.mean(oos)) if oos else np.nan
    d=(ai-ao)/abs(ai) if iss and oos and abs(ai)>0.001 else np.nan
    return {"folds":res,"avg_is_sharpe":round(ai,4) if not np.isnan(ai) else None,
            "avg_oos_sharpe":round(ao,4) if not np.isnan(ao) else None,
            "sharpe_decay":round(d,4) if not np.isnan(d) else None}

def main():
    t0=time.time(); st,ml,di=load_price(); fd=load_fund(); hf=len(fd)>0
    print(f"\nFull bt...",flush=True); t1=time.time(); full=backtest(st,fd,di,0,ml,CONFIG); print(f"  {time.time()-t1:.0f}s",flush=True)
    print(f"\nWF...",flush=True); t2=time.time(); w=wf(st,fd,di,ml,CONFIG); print(f"  {time.time()-t2:.0f}s",flush=True)
    g={}
    if full and full["sharpe_ratio"] is not None: g["S"]=full["sharpe_ratio"]>=1.2; g["M"]=full["max_drawdown"]>=-0.15
    else: g["S"]=g["M"]=False
    g["D"]=(w["sharpe_decay"] is not None and w["sharpe_decay"]<=0.30); g["A"]=all(g.values())
    with open(OUT_DIR/"wf_backtest_v4.4_report.json","w") as f:
        json.dump({"ts":datetime.now().isoformat(),"version":"V4.4",
                   "desc":"V3.5 exact MR + fundamentals overlay (10%)",
                   "has_fundamentals":hf,"n_fund_stocks":len(fd),"cfg":CONFIG,"weights":V44_WEIGHTS,
                   "full":full,"wf":w,"gates":g,"elapsed":round(time.time()-t0,1)},f,indent=2,default=str)
    print(f"\n{'='*60}"); ds=f"V4.4 — V3.5 exact + Fundamentals (PE/PB/PS, {len(fd)} stocks)"
    print(f"  {ds}\n  {len(st)} stocks, top-{CONFIG['top_n']}, {CONFIG['rebalance_freq']}d rebalance\n{'='*60}")
    if full:
        s=full["sharpe_ratio"]; print(f"  Full: Sharpe={'N/A' if s is None else f'{s:.4f}'} Ret={full['annual_return']:.1%} Vol={full['annual_volatility']:.1%} MDD={full['max_drawdown']:.1%}")
        print(f"  Active={full['avg_active_stocks']:.0f} Turnover={full['avg_turnover']:.1%}")
    for fld in w["folds"]:
        iss=f"{fld['is']:.3f}" if fld.get('is') is not None else "N/A"
        oss=fld.get('oos','N/A'); oss=f"{oss:.3f}" if isinstance(oss,float) else oss
        print(f"  F{fld['fold']}: IS={iss} OOS={oss} a={fld.get('act','?')}")
    print(f"  IS:{w['avg_is_sharpe']} OOS:{w['avg_oos_sharpe']} D:{w['sharpe_decay']}")
    print(f"  {'✅' if g['A'] else '❌'} S={'✅' if g['S'] else '❌'} M={'✅' if g['M'] else '❌'} D={'✅' if g['D'] else '❌'}")
    print(f"  ⏱ {time.time()-t0:.0f}s")

if __name__=="__main__": main()
