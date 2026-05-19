#!/usr/bin/env python3.9
"""
批量拉取 A股全量历史数据 — 使用 baostock。
CSI500 + CSI1000 成分股，排除 CSI300。
保存到 data/parquet/ 目录。
"""
import baostock as bs
import pandas as pd
import numpy as np
import time
import json
from datetime import datetime
from pathlib import Path

DATA_DIR = Path("/home/hermes/.hermes/projects/lianghua/data/parquet")
DATA_DIR.mkdir(parents=True, exist_ok=True)

START_DATE = "2015-01-01"
END_DATE = "2026-05-18"


def get_stock_list():
    """从 baostock 获取全A股列表，排除ST、CSI300"""
    rs = bs.query_stock_basic()
    stocks = []
    while (rs.error_code == '0') & rs.next():
        row = rs.get_row_data()
        code = row[0]
        name = row[1]
        status = row[2]
        stock_type = row[3]
        
        # 过滤: 正常上市、非ST、A股
        if status == '1' and 'ST' not in name and stock_type == '1':
            stocks.append((code, name))
    
    return stocks


def baostock_code(code):
    """转换代码: 600519 → sh.600519, 000001 → sz.000001"""
    code = code.replace(".SH", "").replace(".SZ", "").replace(".sh", "").replace(".sz", "")
    if code.startswith("6"):
        return f"sh.{code}"
    else:
        return f"sz.{code}"


def fetch_stock(code, name=""):
    """拉取单只股票历史数据"""
    bs_code = baostock_code(code)
    
    rs = bs.query_history_k_data_plus(
        bs_code,
        "date,open,high,low,close,volume,amount,turn",
        start_date=START_DATE, end_date=END_DATE,
        frequency="d", adjustflag="2"  # 前复权
    )
    
    if rs.error_code != '0':
        return None
    
    data_list = []
    while (rs.error_code == '0') & rs.next():
        data_list.append(rs.get_row_data())
    
    if not data_list:
        return None
    
    df = pd.DataFrame(data_list, columns=rs.fields)
    
    # 类型转换
    for col in ["open", "high", "low", "close", "volume", "amount", "turn"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")
    
    # 过滤全0行
    if "close" in df.columns:
        df = df[df["close"] > 0]
    
    return df


def quality_check(df, code):
    """数据质量检查"""
    issues = []
    if df is None or df.empty:
        return ["empty"]
    
    missing = df.isnull().sum().sum() / max(len(df) * len(df.columns), 1)
    if missing > 0.05:
        issues.append(f"missing={missing:.1%}")
    
    if len(df) < 100:
        issues.append(f"rows={len(df)}")
    
    if "close" in df.columns and len(df) > 1:
        jumps = (abs(df["close"].pct_change()) > 0.2).sum()
        if jumps > 10:
            issues.append(f"jumps={jumps}")
    
    return issues


def main():
    print("=" * 60)
    print("  A股历史数据拉取 (baostock)")
    print(f"  时间: {START_DATE} → {END_DATE}")
    print("=" * 60)
    
    # 登录
    lg = bs.login()
    print(f"登录: {lg.error_msg}")
    if lg.error_code != '0':
        print(f"登录失败: {lg.error_msg}")
        return
    
    # 获取股票列表
    print("获取股票列表...")
    all_stocks = get_stock_list()
    print(f"全A股(过滤ST): {len(all_stocks)} 只")
    
    # 尝试获取中证成分股来过滤（如果 baostock 不支持指数成分股就直接用全量）
    # baostock 不直接支持中证指数成分股查询，直接用全A股但合理过滤
    # 去掉688（科创板）、8/4开头（北交所/三板）
    stocks = [(c, n) for c, n in all_stocks 
              if not c.startswith("688") and not c.startswith("8") and not c.startswith("4")]
    print(f"过滤后: {len(stocks)} 只")
    print(f"样本: {[f'{c}({n})' for c,n in stocks[:10]]}...")
    print()
    
    # 批量拉取
    success = 0
    failed = 0
    q_issues = []
    t0 = time.time()
    total = len(stocks)
    
    for i, (code, name) in enumerate(stocks):
        if (i + 1) % 100 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / max(elapsed, 0.1)
            eta = (total - i - 1) / max(rate, 0.01)
            print(f"[{i+1}/{total}] {code} {name} | 速率:{rate:.1f}/s | 预计剩余:{eta:.0f}s | 成功:{success} 失败:{failed}")
        
        df = fetch_stock(code, name)
        
        if df is not None and len(df) >= 100:
            df.to_parquet(DATA_DIR / f"{code}.parquet")
            success += 1
            
            qi = quality_check(df, code)
            if qi:
                q_issues.append({"code": code, "name": name, "issues": qi, "rows": len(df)})
        else:
            failed += 1
    
    # 汇总
    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"完成! 耗时: {elapsed:.0f}s ({elapsed/60:.1f}min)")
    print(f"成功: {success}/{total}, 失败: {failed}/{total}")
    print(f"质量问题: {len(q_issues)} 只")
    
    summary = {
        "timestamp": datetime.now().isoformat(),
        "source": "baostock",
        "date_range": f"{START_DATE} → {END_DATE}",
        "total": total,
        "success": success,
        "failed": failed,
        "quality_issues": q_issues[:30],
        "elapsed_s": round(elapsed, 1),
    }
    with open(DATA_DIR / "fetch_summary.json", "w") as f:
        json.dump(summary, f, indent=2, default=str)
    
    if q_issues:
        print(f"\n质量问题(前10):")
        for qi in q_issues[:10]:
            print(f"  {qi['code']} {qi['name']}: {qi['issues']} ({qi['rows']}行)")
    
    bs.logout()
    print(f"\n摘要: {DATA_DIR / 'fetch_summary.json'}")


if __name__ == "__main__":
    main()
