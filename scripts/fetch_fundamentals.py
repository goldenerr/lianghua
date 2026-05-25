#!/usr/bin/env python3.9
"""
Fetch fundamental data (PE/PB/PS) for all stocks via baostock.
Aligns with existing price data. Saves to data/fundamentals/.
"""
import baostock as bs
import pandas as pd
import numpy as np
from pathlib import Path
import time, os

from _paths import PROJECT_DIR as PROJECT
from _paths import DATA_DIR
from _paths import FUNDAMENTALS_DIR as FUND_DIR
FUND_DIR.mkdir(parents=True, exist_ok=True)

# Get stock list from existing parquet files
stock_codes = sorted([f.stem for f in DATA_DIR.glob("*.parquet") 
                      if f.stem.isdigit() and len(f.stem) == 6])
print(f"Found {len(stock_codes)} stocks")

def baostock_code(code):
    c = str(code).zfill(6)
    return f"sh.{c}" if c.startswith(("6", "68")) else f"sz.{c}"

bs.login()
print(f"Baostock login: {bs.login().error_code}")

success = 0
fail = 0
empty = 0
t0 = time.time()

# Process in batches to avoid overwhelming the API
for i, code in enumerate(stock_codes):
    if (i + 1) % 50 == 0:
        elapsed = time.time() - t0
        rate = (i + 1) / max(elapsed, 1)
        eta = (len(stock_codes) - i - 1) / max(rate, 0.01)
        print(f"[{i+1}/{len(stock_codes)}] {success} ok, {fail} fail, {empty} empty | "
              f"{elapsed:.0f}s elapsed, ~{eta:.0f}s remaining", flush=True)
    
    out_path = FUND_DIR / f"{code}.parquet"
    if out_path.exists():
        success += 1
        continue
    
    bc = baostock_code(code)
    try:
        rs = bs.query_history_k_data_plus(
            bc, "date,peTTM,pbMRQ,psTTM",
            start_date='2000-01-01', end_date='2026-05-19',
            frequency='d', adjustflag='2')  # 前复权
        
        if rs.error_code != '0':
            fail += 1
            continue
        
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        
        if not rows:
            empty += 1
            continue
        
        df = pd.DataFrame(rows, columns=rs.fields)
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date')
        
        # Convert to numeric
        for col in ['peTTM', 'pbMRQ', 'psTTM']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
            # Replace 0 with NaN (baostock returns 0 for missing)
            df.loc[df[col] == 0, col] = np.nan
        
        df.to_parquet(out_path)
        success += 1
        
    except Exception as e:
        fail += 1
        if fail < 5:
            print(f"  Error {code}: {e}")
    
    # Rate limit
    if i % 10 == 9:
        time.sleep(0.05)

bs.logout()
elapsed = time.time() - t0
print(f"\nDone: {success} ok, {fail} fail, {empty} empty in {elapsed:.0f}s")
