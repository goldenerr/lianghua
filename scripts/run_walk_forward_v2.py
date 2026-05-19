#!/usr/bin/env python3.9
"""
Walk-Forward 回测 v2 — 含风控，AGENTS.md §5 Gate。
修复: OOS空集 NaN 污染 + 加 ATR 止损 + 仓位管理。
"""
import pandas as pd
import numpy as np
import json, time
from pathlib import Path
from datetime import datetime
from collections import defaultdict

DATA_DIR = Path("/home/hermes/.hermes/projects/lianghua/data/parquet")
OUT_DIR = Path("/home/hermes/.hermes/projects/lianghua/data/backtest_results")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── 策略 (含风控) ────────────────────────────────────────
def strategy_with_risk(prices, fast=10, slow=30, atr_period=20, stop_atr=3.0, risk_pct=0.01):
    """SMA crossover + ATR止损 + 1%风险仓位管理"""
    n = len(prices)
    if n < slow + atr_period:
        return {"signal": 0, "position_pct": 0, "stop_price": 0}
    
    # SMA 信号
    fast_ma = np.mean(prices[-fast:])
    slow_ma = np.mean(prices[-slow:])
    signal = 1 if fast_ma > slow_ma else -1
    
    # ATR (Average True Range)
    high = prices[-atr_period:]
    low = prices[-atr_period:]
    tr = np.maximum(high[1:] - low[1:], 
                    np.abs(high[1:] - prices[-atr_period:-1]),
                    np.abs(low[1:] - prices[-atr_period:-1]))
    if len(tr) > 0:
        atr = np.mean(tr[-atr_period//2:]) if len(tr) >= atr_period//2 else np.mean(tr)
    else:
        atr = prices[-1] * 0.02  # 2% fallback
    
    # 止损价
    stop_price = prices[-1] - stop_atr * atr if signal == 1 else prices[-1] + stop_atr * atr
    
    # 仓位 (1% 风险)
    risk_amount = risk_pct
    position_pct = risk_amount / max(stop_atr * atr / prices[-1], 0.001)
    position_pct = min(max(position_pct, 0), 1.0)
    
    return {"signal": signal, "position_pct": position_pct, "stop_price": stop_price}

# ── 回测 ──────────────────────────────────────────────────
def backtest_v2(prices, strategy_fn):
    """Bar-by-bar 回测，含止损和仓位管理"""
    n = len(prices)
    warmup = 60
    if n < warmup:
        return None
    
    positions = np.zeros(n)
    stops_hit = 0
    in_position = False
    entry_price = 0
    stop_price = 0
    
    for i in range(warmup, n):
        s = strategy_fn(prices[:i+1])
        signal = s["signal"]
        pos_pct = s["position_pct"]
        
        # 止损检查
        if in_position:
            if signal == 1 and prices[i] <= stop_price:
                positions[i] = 0
                in_position = False
                stops_hit += 1
            elif signal == -1 and prices[i] >= stop_price:
                positions[i] = 0
                in_position = False
                stops_hit += 1
            else:
                positions[i] = positions[i-1]
        else:
            if signal != 0 and pos_pct > 0.01:
                positions[i] = signal * pos_pct
                in_position = True
                entry_price = prices[i]
                stop_price = s["stop_price"]
            else:
                positions[i] = 0
    
    # 计算收益
    rets = np.diff(prices) / prices[:-1]
    strat_rets = positions[warmup:-1] * rets[warmup:]
    
    if len(strat_rets) < 50:
        return None
    
    # 过滤极端值
    strat_rets = np.clip(strat_rets, -0.15, 0.15)
    
    equity = np.cumprod(1 + strat_rets)
    total_ret = equity[-1] - 1
    ann_ret = np.mean(strat_rets) * 252
    ann_vol = np.std(strat_rets, ddof=1) * np.sqrt(252)
    sharpe = (ann_ret - 0.02) / max(ann_vol, 0.01)
    
    peak = np.maximum.accumulate(equity)
    mdd = np.min((equity - peak) / np.maximum(peak, 0.01))
    calmar = ann_ret / max(abs(mdd), 0.01)
    win_rate = np.mean(strat_rets > 0)
    
    n_trades = int(np.sum(np.abs(np.diff(positions[warmup:]))) / 2)
    
    return {
        "total_return": float(total_ret),
        "ann_return": float(ann_ret),
        "sharpe": float(sharpe),
        "max_drawdown": float(mdd),
        "calmar": float(calmar),
        "win_rate": float(win_rate),
        "n_trades": n_trades,
        "stops_hit": stops_hit,
        "ann_vol": float(ann_vol),
    }

# ── Walk-Forward ─────────────────────────────────────────
def walk_forward_v2(prices, strategy_fn, n_folds=5):
    """Walk-forward。训练 = 滚动扩展窗口，测试 = 下一段。鲁棒处理边缘情况。"""
    n = len(prices)
    if n < 500:
        return None
    
    fold_size = n // (n_folds + 1)
    is_sharpes = []
    oos_sharpes = []
    is_mdds = []
    oos_mdds = []
    
    for fold in range(1, n_folds + 1):
        split = fold * fold_size
        if split > n - 50:
            break
        
        train_prices = prices[:split]
        test_prices = prices[split:min(split + fold_size, n)]
        
        if len(train_prices) < 200 or len(test_prices) < 50:
            continue
        
        train_bt = backtest_v2(train_prices, strategy_fn)
        test_bt = backtest_v2(test_prices, strategy_fn)
        
        if train_bt and test_bt:
            is_sharpes.append(train_bt["sharpe"])
            oos_sharpes.append(test_bt["sharpe"])
            is_mdds.append(abs(train_bt["max_drawdown"]))
            oos_mdds.append(abs(test_bt["max_drawdown"]))
    
    if not is_sharpes or not oos_sharpes:
        return None
    
    # 过滤异常值 (|Sharpe| > 10 为计算错误)
    valid = [(is_s, oos_s) for is_s, oos_s in zip(is_sharpes, oos_sharpes)
             if abs(is_s) < 10 and abs(oos_s) < 10]
    
    if not valid:
        return None
    
    is_vals = [v[0] for v in valid]
    oos_vals = [v[1] for v in valid]
    
    avg_is = np.mean(is_vals)
    avg_oos = np.mean(oos_vals)
    decay = max(0, (avg_is - avg_oos) / max(abs(avg_is), 0.01)) if abs(avg_is) > 0.01 else 0
    
    return {
        "avg_is_sharpe": round(avg_is, 4),
        "avg_oos_sharpe": round(avg_oos, 4),
        "sharpe_decay": round(float(decay), 4),
        "n_valid_folds": len(valid),
    }

# ── 主流程 ────────────────────────────────────────────────
def main():
    files = sorted(DATA_DIR.glob("*.parquet"))
    print(f"加载 {len(files)} 只股票...", flush=True)
    t0 = time.time()
    
    full_data = {}
    wf_data = {}
    errors = 0
    
    for i, f in enumerate(files):
        if (i+1) % 200 == 0:
            print(f"[{i+1}/{len(files)}] {len(full_data)} 有效 | {time.time()-t0:.0f}s", flush=True)
        
        try:
            df = pd.read_parquet(f)
            prices = df['close'].values
            code = f.stem
            
            if len(prices) < 252:
                continue
            
            def strat(p):
                return strategy_with_risk(p)
            
            bt = backtest_v2(prices, strat)
            if bt:
                full_data[code] = bt
            
            wf = walk_forward_v2(prices, strat)
            if wf:
                wf_data[code] = wf
            
        except Exception:
            errors += 1
    
    elapsed = time.time() - t0
    n_bt = len(full_data)
    n_wf = len(wf_data)
    
    if n_bt == 0:
        print("ERROR: 0 有效!", flush=True)
        return
    
    # ── 统计 ────────────────────────────────────────────
    sharpes = np.array([d["sharpe"] for d in full_data.values()])
    mdds = np.array([abs(d["max_drawdown"]) for d in full_data.values()])
    calmar = np.array([d["calmar"] for d in full_data.values()])
    winrates = np.array([d["win_rate"] for d in full_data.values()])
    
    # WF
    wf_oop_sharpes = np.array([w["avg_oos_sharpe"] for w in wf_data.values()])
    wf_decays = np.array([w["sharpe_decay"] for w in wf_data.values()])
    
    # ── 报告 ────────────────────────────────────────────
    def pct(arr, thresh):
        return round(float(np.mean(arr >= thresh)), 4) if len(arr) > 0 else 0
    
    gates = {
        "sharpe_mean_ge_1.2": float(np.mean(sharpes)) >= 1.2,
        "sharpe_median_ge_0.8": float(np.median(sharpes)) >= 0.8,
        "mdd_median_le_15pct": float(np.median(mdds)) <= 0.15,
        "mdd_median_le_25pct": float(np.median(mdds)) <= 0.25,
        "wf_decay_le_30pct": float(np.mean(wf_decays)) <= 0.30,
        "wf_oos_sharpe_positive": float(np.mean(wf_oop_sharpes)) > 0,
    }
    gates["all_passed"] = all(gates.values())
    
    report = {
        "timestamp": datetime.now().isoformat(),
        "data": {"source":"akshare/sina","range":"2000-2026","stocks":len(files)},
        "full_backtest": {
            "valid": n_bt,
            "mean_sharpe": round(float(np.mean(sharpes)), 4),
            "median_sharpe": round(float(np.median(sharpes)), 4),
            "sharpe_gt_1": pct(sharpes, 1.0),
            "sharpe_gt_1.2": pct(sharpes, 1.2),
            "mean_mdd": round(float(np.mean(mdds)), 4),
            "median_mdd": round(float(np.median(mdds)), 4),
            "mdd_lt_15pct": pct(-mdds, -0.15),
            "mdd_lt_25pct": pct(-mdds, -0.25),
            "mean_calmar": round(float(np.mean(calmar)), 4),
            "mean_win_rate": round(float(np.mean(winrates)), 4),
        },
        "walk_forward": {
            "valid": n_wf,
            "mean_oos_sharpe": round(float(np.mean(wf_oop_sharpes)), 4),
            "mean_sharpe_decay": round(float(np.mean(wf_decays)), 4),
            "decay_lt_30pct": pct(-wf_decays, -0.30),
        },
        "gates": gates,
        "elapsed_s": round(elapsed, 1),
    }
    
    with open(OUT_DIR/"wf_report_v2.json","w") as f:
        json.dump(report, f, indent=2, default=str)
    
    # 打印
    print(f"\n{'='*60}")
    print(f"  全量 Walk-Forward v2 (含ATR止损+仓位)")
    print(f"  数据: 26年 A股 {len(files)}只")
    print(f"{'='*60}")
    print(f"  全量回测: {n_bt} 只 | WF: {n_wf} 只 | 耗时: {elapsed:.0f}s")
    print(f"")
    print(f"  全量:")
    print(f"    Sharpe 均值:  {report['full_backtest']['mean_sharpe']:.4f}")
    print(f"    Sharpe 中位:  {report['full_backtest']['median_sharpe']:.4f}")
    print(f"    Sharpe>1:     {report['full_backtest']['sharpe_gt_1']*100:.0f}%")
    print(f"    MDD 中位:     {report['full_backtest']['median_mdd']:.1%}")
    print(f"    MDD<25%:      {report['full_backtest']['mdd_lt_25pct']*100:.0f}%")
    print(f"")
    print(f"  Walk-Forward:")
    print(f"    OOS Sharpe:   {report['walk_forward']['mean_oos_sharpe']:.4f}")
    print(f"    Sharpe衰减:   {report['walk_forward']['mean_sharpe_decay']:.1%}")
    print(f"    衰减<30%:     {report['walk_forward']['decay_lt_30pct']*100:.0f}%")
    print(f"")
    print(f"  Gate:")
    for g, v in gates.items():
        print(f"    {'✅' if v else '❌'} {g}")

if __name__ == "__main__":
    main()
