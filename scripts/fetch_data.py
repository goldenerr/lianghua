#!/usr/bin/env python3.9
"""
批量拉取 A股全量历史数据 — CSI500 + CSI1000 成分股。
保存到 data/parquet/ 目录，按 stock_code.parquet 分区存储。
"""
import akshare as ak
import pandas as pd
import numpy as np
import time
import os
from datetime import datetime
from pathlib import Path

from _paths import DATA_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)

START_DATE = "20150101"  # 2015年起 — 覆盖完整牛熊周期
END_DATE = "20260518"

def get_stock_universe():
    """获取 CSI500 + CSI1000 成分股代码列表（剔除 CSI300）"""
    stocks = set()
    
    # CSI500 成分股
    try:
        df500 = ak.index_stock_cons(symbol="000905")  # 中证500
        for code in df500["品种代码"].values:
            stocks.add(str(code))
        print(f"CSI500: {len(df500)} 只")
    except Exception as e:
        print(f"CSI500 获取失败: {e}")
    
    # CSI1000 成分股
    try:
        df1000 = ak.index_stock_cons(symbol="000852")  # 中证1000
        for code in df1000["品种代码"].values:
            stocks.add(str(code))
        print(f"CSI1000: {len(df1000)} 只")
    except Exception as e:
        print(f"CSI1000 获取失败: {e}")
    
    # 如果上面都失败，退而求其次用全A股列表
    if len(stocks) == 0:
        print("指数成分股获取失败，使用全A股列表...")
        try:
            df_all = ak.stock_zh_a_spot_em()
            # 过滤ST、科创板等
            codes = []
            for _, row in df_all.iterrows():
                code = row["代码"]
                name = row["名称"]
                if "ST" not in name and not code.startswith("688"):
                    codes.append(code)
            print(f"全A股(过滤ST/688): {len(codes)} 只")
            return codes
        except Exception as e:
            print(f"全A股列表获取也失败: {e}")
            return []
    
    return sorted(stocks)


def fetch_stock(code, retries=3):
    """拉取单只股票历史数据，带重试"""
    for attempt in range(retries):
        try:
            df = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=START_DATE,
                end_date=END_DATE,
                adjust="qfq",
            )
            if df is None or df.empty:
                return None
            
            # 标准化列名
            col_map = {
                "日期": "date", "开盘": "open", "最高": "high",
                "最低": "low", "收盘": "close", "成交量": "volume",
                "成交额": "amount", "换手率": "turnover_rate",
                "振幅": "amplitude", "涨跌幅": "pct_change",
                "涨跌额": "change", "股票代码": "symbol",
            }
            df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
            
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
                df = df.set_index("date")
            
            # 只保留关键列
            key_cols = ["open", "high", "low", "close", "volume", "amount", "turnover_rate"]
            df = df[[c for c in key_cols if c in df.columns]]
            
            time.sleep(0.3)  # 限速，避免被封
            return df
            
        except Exception as e:
            if attempt < retries - 1:
                wait = (attempt + 1) * 2
                print(f"  {code} 重试 {attempt+1}/{retries} (等待{wait}s): {e}")
                time.sleep(wait)
            else:
                print(f"  {code} 失败: {e}")
                return None


def quality_check(df, code):
    """数据质量检查"""
    issues = []
    if df is None or df.empty:
        return ["empty"]
    
    # 缺失值比例
    missing = df.isnull().sum().sum() / (len(df) * len(df.columns))
    if missing > 0.01:
        issues.append(f"missing={missing:.2%}")
    
    # 价格跳空
    if "close" in df.columns and len(df) > 1:
        pct_changes = df["close"].pct_change().dropna()
        jumps = (abs(pct_changes) > 0.2).sum()
        if jumps > 0:
            issues.append(f"price_jumps={jumps}")
    
    # 行数检查（至少要有100个交易日）
    if len(df) < 100:
        issues.append(f"rows={len(df)}")
    
    return issues


def main():
    print("=== 批量拉取 A股历史数据 ===")
    print(f"时间范围: {START_DATE} → {END_DATE}")
    print(f"存储路径: {DATA_DIR}")
    print()
    
    # 1. 获取股票池
    stocks = get_stock_universe()
    total = len(stocks)
    print(f"\n股票池: {total} 只")
    if total == 0:
        print("无法获取股票列表，退出")
        return
    print(f"样本: {stocks[:10]}...")
    print()
    
    # 2. 批量拉取
    success = 0
    failed = 0
    quality_issues = []
    start_time = time.time()
    
    for i, code in enumerate(stocks):
        if (i + 1) % 50 == 0 or i == 0:
            elapsed = time.time() - start_time
            eta = (elapsed / max(i, 1)) * (total - i)
            print(f"[{i+1}/{total}] {code}... (已用{elapsed:.0f}s, 预计剩余{eta:.0f}s)")
        
        df = fetch_stock(code)
        
        if df is not None and not df.empty:
            path = DATA_DIR / f"{code}.parquet"
            df.to_parquet(path)
            success += 1
            
            issues = quality_check(df, code)
            if issues:
                quality_issues.append({"code": code, "issues": issues, "rows": len(df)})
        else:
            failed += 1
    
    # 3. 汇总
    elapsed = time.time() - start_time
    print(f"\n{'='*60}")
    print(f"拉取完成! 耗时: {elapsed:.0f}s ({elapsed/60:.1f}min)")
    print(f"成功: {success}/{total}, 失败: {failed}/{total}")
    print(f"数据质量问题: {len(quality_issues)} 只")
    print(f"文件路径: {DATA_DIR}")
    
    # 4. 保存汇总
    summary = {
        "timestamp": datetime.now().isoformat(),
        "date_range": f"{START_DATE} → {END_DATE}",
        "total_stocks": total,
        "success": success,
        "failed": failed,
        "quality_issues": quality_issues[:20],  # 只记录前20个
        "elapsed_seconds": elapsed,
    }
    import json
    with open(DATA_DIR / "fetch_summary.json", "w") as f:
        json.dump(summary, f, indent=2, default=str)
    
    # 5. 质量报告
    if quality_issues:
        print(f"\n数据质量问题详情 (前10):")
        for qi in quality_issues[:10]:
            print(f"  {qi['code']}: {qi['issues']} ({qi['rows']}行)")


if __name__ == "__main__":
    main()
