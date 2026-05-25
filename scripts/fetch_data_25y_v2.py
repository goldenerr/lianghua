#!/usr/bin/env python3.9 -u
"""
批量拉取 A股 25 年历史数据 — akshare 新浪数据源。
CSI500+1000 成分股 (~1200只)，2000-01-01 起。
每只间隔 2 秒，预计 40 分钟。
"""
import akshare as ak
import pandas as pd
import numpy as np
import time, json, sys
from datetime import datetime
from pathlib import Path

from _paths import DATA_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)
from _paths import STOCK_LIST

START_DATE = "20000101"
END_DATE = "20260518"

def load_codes():
    with open(STOCK_LIST) as f:
        return json.load(f)

def to_sina(code):
    """600519 → sh600519, 000001 → sz000001"""
    c = code.replace(".SH","").replace(".SZ","").replace(".sh","").replace(".sz","")
    return f"sh{c}" if c.startswith("6") else f"sz{c}"

def fetch_one(code):
    sym = to_sina(code)
    try:
        df = ak.stock_zh_a_daily(symbol=sym, start_date=START_DATE, end_date=END_DATE, adjust='qfq')
        if df is None or df.empty or len(df) < 50:
            return None
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date').sort_index()
        # 只保留关键列
        cols = ['open','high','low','close','volume','amount','turnover']
        return df[[c for c in cols if c in df.columns]]
    except Exception as e:
        return None

def main():
    codes = load_codes()
    total = len(codes)
    print(f"股票池: {total} 只 | 数据源: akshare/sina", flush=True)
    
    ok = fail = 0
    t0 = time.time()
    
    for i, code in enumerate(codes):
        if (i+1) % 100 == 0 or i < 5:
            elapsed = time.time()-t0
            rate = (i+1)/max(elapsed,0.1)
            eta = (total-i-1)/max(rate,0.01)
            print(f"[{i+1}/{total}] ok={ok} fail={fail} | {rate:.1f}/min | ETA {eta/60:.0f}min", flush=True)
        
        df = fetch_one(code)
        if df is not None:
            df.to_parquet(DATA_DIR / f"{code}.parquet")
            ok += 1
        else:
            fail += 1
        
        time.sleep(2)  # 限速
    
    elapsed = time.time()-t0
    
    # 统计
    total_rows = 0
    d_min, d_max = "", ""
    for f in DATA_DIR.glob("*.parquet"):
        try:
            df = pd.read_parquet(f)
            total_rows += len(df)
            if not d_min or df.index.min() < pd.Timestamp(d_min): d_min = str(df.index.min().date())
            if not d_max or df.index.max() > pd.Timestamp(d_max): d_max = str(df.index.max().date())
        except: pass
    
    print(f"\n✅ 完成! {elapsed:.0f}s ({elapsed/60:.1f}min)")
    print(f"成功: {ok}/{total}  失败: {fail}")
    print(f"总行数: {total_rows:,}")
    print(f"日期: {d_min} → {d_max}")
    
    summary = {
        "timestamp": datetime.now().isoformat(),
        "source": "akshare/sina",
        "start": START_DATE, "end": END_DATE,
        "actual_range": f"{d_min} → {d_max}",
        "total": total, "success": ok, "failed": fail,
        "total_rows": total_rows,
        "elapsed_s": round(elapsed,1),
    }
    with open(DATA_DIR/"fetch_summary.json","w") as f:
        json.dump(summary, f, indent=2)

if __name__ == "__main__":
    main()
