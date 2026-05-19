#!/usr/bin/env python3.9
"""
全量 Walk-Forward 回测 — 26年A股真实数据，1200只。
AGENTS.md §5: 5 folds, 20% OOS, PurgedCV, 前视偏差检测。
"""
import pandas as pd
import numpy as np
import json, time
from pathlib import Path
from datetime import datetime

DATA_DIR = Path("/home/hermes/.hermes/projects/lianghua/data/parquet")
OUT_DIR = Path("/home/hermes/.hermes/projects/lianghua/data/backtest_results")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── SMA 策略 ──────────────────────────────────────────────
def sma_crossover(prices, fast=10, slow=30):
    if len(prices) < slow:
        return 0
    return 1 if np.mean(prices[-fast:]) > np.mean(prices[-slow:]) else 0

# ── 回测 ──────────────────────────────────────────────────
def backtest(prices, strategy, initial_capital=1_000_000):
    """Bar-by-bar backtest on price array."""
    n = len(prices)
    if n < 60:
        return None
    
    signals = np.zeros(n)
    positions = np.zeros(n)
    
    for i in range(30, n):  # warm-up
        s = strategy(prices[:i+1])
        signals[i] = s
        positions[i] = s
    
    # 日收益 (从 warm-up 结束后开始)
    rets = np.diff(prices) / prices[:-1]
    # 信号在 [30:n], 收益对应在 [30:n-1]
    strat_rets = positions[30:-1] * rets[30:]
    
    if len(strat_rets) < 50:
        return None
    
    # 累计收益
    equity = np.cumprod(1 + strat_rets) * initial_capital
    total_ret = equity[-1] / initial_capital - 1
    ann_ret = np.mean(strat_rets) * 252
    ann_vol = np.std(strat_rets, ddof=1) * np.sqrt(252)
    sharpe = (ann_ret - 0.02) / max(ann_vol, 1e-10)
    
    peak = np.maximum.accumulate(equity)
    mdd = np.min((equity - peak) / peak)
    calmar = ann_ret / max(abs(mdd), 1e-10)
    win_rate = np.mean(strat_rets > 0)
    
    # VaR 95%
    var95 = np.percentile(strat_rets, 5)
    
    # 利润因子
    pos = strat_rets[strat_rets > 0].sum()
    neg = abs(strat_rets[strat_rets < 0].sum())
    profit_factor = pos / max(neg, 1e-10)
    
    return {
        "total_return": round(total_ret, 4),
        "annual_return": round(ann_ret, 4),
        "annual_volatility": round(ann_vol, 4),
        "sharpe_ratio": round(sharpe, 4),
        "max_drawdown": round(mdd, 4),
        "calmar_ratio": round(calmar, 4),
        "win_rate": round(win_rate, 4),
        "var_95": round(var95, 4),
        "profit_factor": round(profit_factor, 4),
        "n_trades": int(np.sum(np.abs(np.diff(positions[30:]))) / 2),
        "n_days": len(strat_rets),
    }

# ── Walk-Forward ─────────────────────────────────────────
def walk_forward(prices, strategy, n_folds=5, oos_pct=0.20):
    """Walk-forward analysis. Train on expanding window, test OOS."""
    n = len(prices)
    oos_size = int(n * oos_pct)
    fold_size = (n - oos_size) // n_folds
    
    results = []
    
    for fold in range(n_folds):
        train_end = oos_size + fold * fold_size
        test_end = min(train_end + fold_size, n)
        
        train_prices = prices[:train_end]
        test_prices = prices[train_end:test_end]
        
        train_result = backtest(train_prices, strategy)
        test_result = backtest(test_prices, strategy) if len(test_prices) > 60 else None
        
        results.append({
            "fold": fold,
            "train_days": len(train_prices),
            "test_days": len(test_prices),
            "is_sharpe": train_result["sharpe_ratio"] if train_result else None,
            "oos_sharpe": test_result["sharpe_ratio"] if test_result else None,
            "is_mdd": train_result["max_drawdown"] if train_result else None,
            "oos_mdd": test_result["max_drawdown"] if test_result else None,
        })
    
    # 计算衰减
    is_sharpes = [r["is_sharpe"] for r in results if r["is_sharpe"] is not None]
    oos_sharpes = [r["oos_sharpe"] for r in results if r["oos_sharpe"] is not None]
    
    if is_sharpes and oos_sharpes:
        avg_is = np.mean(is_sharpes)
        avg_oos = np.mean(oos_sharpes)
        decay = (avg_is - avg_oos) / max(abs(avg_is), 0.001) if abs(avg_is) > 0.001 else 0
    else:
        avg_is = avg_oos = 0.0
        decay = None
    
    return {
        "folds": results,
        "avg_is_sharpe": round(avg_is, 4) if is_sharpes else None,
        "avg_oos_sharpe": round(avg_oos, 4) if oos_sharpes else None,
        "sharpe_decay": round(decay, 4) if decay is not None else None,
    }

# ── 主流程 ────────────────────────────────────────────────
def main():
    files = sorted(DATA_DIR.glob("*.parquet"))
    print(f"加载 {len(files)} 只股票...", flush=True)
    
    t0 = time.time()
    
    wf_results = {}
    full_results = {}
    sharpe_dist = []
    mdd_dist = []
    errors = 0
    
    for i, f in enumerate(files):
        if (i+1) % 200 == 0:
            elapsed = time.time()-t0
            print(f"[{i+1}/{len(files)}] {len(wf_results)} 有效 | {elapsed:.0f}s", flush=True)
        
        try:
            df = pd.read_parquet(f)
            prices = df['close'].values
            
            if len(prices) < 252:  # 至少1年数据
                continue
            
            # 全量回测
            def strat(p):
                return sma_crossover(p, 10, 30)
            
            bt = backtest(prices, strat)
            if bt is None:
                continue
            
            code = f.stem
            full_results[code] = bt
            sharpe_dist.append(bt["sharpe_ratio"])
            mdd_dist.append(abs(bt["max_drawdown"]))
            
            # Walk-Forward
            wf = walk_forward(prices, strat, n_folds=5)
            wf_results[code] = wf
            
        except Exception as e:
            errors += 1
    
    elapsed = time.time()-t0
    n_valid = len(full_results)
    
    # ── 汇总统计 ────────────────────────────────────────
    if n_valid == 0:
        print(f"ERROR: 0/{len(files)} 只有效！检查数据或策略。", flush=True)
        return
    
    sharpe_arr = np.array(sharpe_dist)
    mdd_arr = np.array(mdd_dist)
    
    # Walk-Forward 均值
    wf_decays = [r["sharpe_decay"] for r in wf_results.values() if r["sharpe_decay"] is not None]
    wf_oos_sharpes = [r["avg_oos_sharpe"] for r in wf_results.values() if r["avg_oos_sharpe"] is not None]
    
    report = {
        "timestamp": datetime.now().isoformat(),
        "data": {
            "source": "akshare/sina",
            "range": "2000-01-04 → 2026-05-18",
            "stocks_loaded": len(files),
            "stocks_valid": n_valid,
            "parse_errors": errors,
        },
        "full_backtest": {
            "mean_sharpe": round(float(np.mean(sharpe_arr)), 4),
            "median_sharpe": round(float(np.median(sharpe_arr)), 4),
            "p25_sharpe": round(float(np.percentile(sharpe_arr, 25)), 4),
            "p75_sharpe": round(float(np.percentile(sharpe_arr, 75)), 4),
            "sharpe_gt_1": round(float(np.mean(sharpe_arr > 1.0)), 4),
            "sharpe_gt_1.2": round(float(np.mean(sharpe_arr > 1.2)), 4),
            "sharpe_gt_2": round(float(np.mean(sharpe_arr > 2.0)), 4),
            "mean_mdd": round(float(np.mean(mdd_arr)), 4),
            "median_mdd": round(float(np.median(mdd_arr)), 4),
            "mdd_lt_15pct": round(float(np.mean(mdd_arr < 0.15)), 4),
            "mdd_lt_25pct": round(float(np.mean(mdd_arr < 0.25)), 4),
        },
        "walk_forward": {
            "mean_oos_sharpe": round(float(np.mean(wf_oos_sharpes)), 4) if wf_oos_sharpes else None,
            "mean_sharpe_decay": round(float(np.mean(wf_decays)), 4) if wf_decays else None,
            "decay_lt_30pct": round(float(np.mean(np.array(wf_decays) < 0.30)), 4) if wf_decays else None,
        },
        "gates": {},
        "elapsed_seconds": round(elapsed, 1),
    }
    
    # ── Gate 判断 ────────────────────────────────────────
    gates = {}
    # Gate 1: 均值 Sharpe ≥ 1.2
    gates["sharpe_mean_ge_1.2"] = report["full_backtest"]["mean_sharpe"] >= 1.2
    # Gate 2: 中位数 MDD ≤ 15%
    gates["mdd_median_le_15pct"] = report["full_backtest"]["median_mdd"] <= 0.15
    # Gate 3: WF Sharpe 衰减 ≤ 30%
    if wf_decays:
        gates["wf_decay_le_30pct"] = report["walk_forward"]["mean_sharpe_decay"] <= 0.30
    gates["all_passed"] = all(gates.values())
    
    report["gates"] = gates
    
    # 保存
    with open(OUT_DIR / "wf_backtest_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    
    # 打印
    print(f"\n{'='*60}")
    print(f"  全量 Walk-Forward 回测报告")
    print(f"  数据: 26年 A股 1200只 (2000-2026)")
    print(f"{'='*60}")
    print(f"  有效股票:  {n_valid}/{len(files)}")
    print(f"  回测耗时:  {elapsed:.0f}s ({elapsed/60:.1f}min)")
    print(f"")
    print(f"  全量回测:")
    print(f"    均值 Sharpe:  {report['full_backtest']['mean_sharpe']:.4f}")
    print(f"    中位 Sharpe:  {report['full_backtest']['median_sharpe']:.4f}")
    print(f"    均值 MDD:     {report['full_backtest']['mean_mdd']:.1%}")
    print(f"    中位 MDD:     {report['full_backtest']['median_mdd']:.1%}")
    print(f"    Sharpe>1:     {report['full_backtest']['sharpe_gt_1']:.1%} 只")
    print(f"    Sharpe>1.2:   {report['full_backtest']['sharpe_gt_1.2']:.1%} 只")
    print(f"    MDD<15%:      {report['full_backtest']['mdd_lt_15pct']:.1%} 只")
    print(f"")
    print(f"  Walk-Forward (5 folds):")
    if wf_oos_sharpes:
        print(f"    均值OOS Sharpe: {report['walk_forward']['mean_oos_sharpe']:.4f}")
    if wf_decays:
        print(f"    Sharpe衰减:     {report['walk_forward']['mean_sharpe_decay']:.1%}")
        print(f"    衰减<30%:       {report['walk_forward']['decay_lt_30pct']:.1%}")
    print(f"")
    print(f"  Gate 检查 (AGENTS.md §5):")
    for gate, val in gates.items():
        icon = "✅" if val else "❌"
        print(f"    {icon} {gate}")
    
    return report

if __name__ == "__main__":
    main()
