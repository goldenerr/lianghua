#!/usr/bin/env python3.9
"""
批量拉取 A股 25 年历史数据 — baostock。
CSI500+1000 成分股 (~1200只)，2000-01-01 起。
"""
import baostock as bs
import pandas as pd
import numpy as np
import time
import json
from datetime import datetime
from pathlib import Path

from _paths import DATA_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)
from _paths import STOCK_LIST

START_DATE = "2000-01-01"
END_DATE = "2026-05-18"


def load_stock_list():
    with open(STOCK_LIST) as f:
        codes = json.load(f)
    return codes


def to_baostock(code):
    """600519 → sh.600519, 000001 → sz.000001"""
    c = code.replace(".SH", "").replace(".SZ", "").replace(".sh", "").replace(".sz", "")
    return f"sh.{c}" if c.startswith("6") else f"sz.{c}"


def fetch_one(code):
    bs_code = to_baostock(code)
    rs = bs.query_history_k_data_plus(
        bs_code,
        "date,open,high,low,close,volume,amount,turn",
        start_date=START_DATE, end_date=END_DATE,
        frequency="d", adjustflag="2",
    )
    if rs.error_code != '0':
        return None
    
    rows = []
    while (rs.error_code == '0') & rs.next():
        rows.append(rs.get_row_data())
    
    if not rows or len(rows) < 50:
        return None
    
    df = pd.DataFrame(rows, columns=rs.fields)
    for col in ["open", "high", "low", "close", "volume", "amount", "turn"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    df = df[df["close"] > 0]
    return df if len(df) >= 50 else None


def main():
    print("=" * 60)
    print(f"  A股 25年历史数据 → {DATA_DIR}")
    print(f"  时间: {START_DATE} → {END_DATE}")
    print("=" * 60)
    
    codes = load_stock_list()
    total = len(codes)
    print(f"股票池: {total} 只")
    
    bs.login()
    t0 = time.time()
    ok, fail = 0, 0
    year_counts = {}
    
    for i, code in enumerate(codes):
        if (i + 1) % 200 == 0 or i == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / max(elapsed, 0.1)
            eta = (total - i - 1) / max(rate, 0.01)
            print(f"[{i+1}/{total}] ok={ok} fail={fail} | {rate:.1f}/s | 剩余{eta:.0f}s")
        
        df = fetch_one(code)
        if df is not None:
            df.to_parquet(DATA_DIR / f"{code}.parquet")
            ok += 1
            yr = df.index.year.min()
            year_counts[yr] = year_counts.get(yr, 0) + 1
        else:
            fail += 1
    
    elapsed = time.time() - t0
    bs.logout()
    
    # 统计
    total_rows = 0
    date_range = ("", "")
    for f in DATA_DIR.glob("*.parquet"):
        try:
            df = pd.read_parquet(f)
            total_rows += len(df)
            if not date_range[0] or df.index.min() < pd.Timestamp(date_range[0]):
                date_range = (str(df.index.min().date()), date_range[1])
            if not date_range[1] or df.index.max() > pd.Timestamp(date_range[1]):
                date_range = (date_range[0], str(df.index.max().date()))
        except:
            pass
    
    print(f"\n{'='*60}")
    print(f"✅ 完成! {elapsed:.1f}s ({elapsed/60:.1f}min)")
    print(f"成功: {ok}/{total}, 失败: {fail}/{total}")
    print(f"总行数: {total_rows:,}")
    print(f"日期范围: {date_range[0]} → {date_range[1]}")
    print(f"年份覆盖: {dict(sorted(year_counts.items())[:5])} ...")
    
    summary = {
        "timestamp": datetime.now().isoformat(),
        "source": "baostock",
        "date_range": f"{START_DATE} → {END_DATE}",
        "actual_range": f"{date_range[0]} → {date_range[1]}",
        "total": total, "success": ok, "failed": fail,
        "total_rows": total_rows,
        "elapsed_s": round(elapsed, 1),
        "year_counts": year_counts,
    }
    with open(DATA_DIR / "fetch_summary.json", "w") as f:
        json.dump(summary, f, indent=2, default=str)


if __name__ == "__main__":
    main()
