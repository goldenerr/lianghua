import pandas as pd, numpy as np
from pathlib import Path

DATA_DIR = Path("/home/hermes/.hermes/projects/lianghua/data/parquet")

f = sorted(DATA_DIR.glob("*.parquet"))[100]
df = pd.read_parquet(f)
prices = df['close'].values
print(f"Stock: {f.stem}, days: {len(prices)}")

def sma_crossover(prices, fast=10, slow=30):
    if len(prices) < slow: return 0
    return 1 if np.mean(prices[-fast:]) > np.mean(prices[-slow:]) else 0

def backtest(prices, strategy):
    n = len(prices)
    if n < 60: return None
    positions = np.zeros(n)
    for i in range(30, n):
        s = strategy(prices[:i+1])
        positions[i] = s
    rets = np.diff(prices) / prices[:-1]
    strat_rets = positions[30:-1] * rets[30:]
    if len(strat_rets) < 50:
        return None
    ann_ret = np.mean(strat_rets) * 252
    ann_vol = np.std(strat_rets, ddof=1) * np.sqrt(252)
    sharpe = (ann_ret - 0.02) / max(ann_vol, 1e-10)
    n_trades = int(np.sum(np.abs(np.diff(positions[30:]))) / 2)
    return {'sharpe': sharpe, 'ann_ret': ann_ret, 'ann_vol': ann_vol,
            'n_trades': n_trades, 'n_days': len(strat_rets)}

def walk_forward(prices, strategy, n_folds=5, oos_pct=0.20):
    n = len(prices)
    oos_size = int(n * oos_pct)
    fold_size = (n - oos_size) // n_folds
    for fold in range(n_folds):
        train_end = oos_size + fold * fold_size
        test_end = min(train_end + fold_size, n)
        train_prices = prices[:train_end]
        test_prices = prices[train_end:test_end]
        train_result = backtest(train_prices, strategy)
        test_result = backtest(test_prices, strategy) if len(test_prices) > 60 else None
        print(f"  Fold {fold}: train={len(train_prices)}d test={len(test_prices)}d")
        if train_result:
            print(f"    Train: sharpe={train_result['sharpe']:.2f} vol={train_result['ann_vol']:.6f} trades={train_result['n_trades']}")
        else:
            print(f"    Train: None")
        if test_result:
            print(f"    Test:  sharpe={test_result['sharpe']:.2f} vol={test_result['ann_vol']:.6f} trades={test_result['n_trades']}")
        else:
            print(f"    Test: None")

def strat(p):
    return sma_crossover(p, 10, 30)

walk_forward(prices, strat)

# Also test 5 random stocks, count zero-trade folds
print("\n=== ZERO-TRADE SURVEY (20 stocks) ===")
zero_trade_folds = 0
total_folds = 0
zero_vol_folds = 0
for f in sorted(DATA_DIR.glob("*.parquet"))[:20]:
    df = pd.read_parquet(f)
    prices = df['close'].values
    n = len(prices)
    oos_size = int(n * 0.20)
    fold_size = (n - oos_size) // 5
    for fold in range(5):
        train_end = oos_size + fold * fold_size
        test_end = min(train_end + fold_size, n)
        test_prices = prices[train_end:test_end]
        bt = backtest(test_prices, strat)
        if bt and bt['n_trades'] == 0:
            zero_trade_folds += 1
        if bt and bt['ann_vol'] < 1e-8:
            zero_vol_folds += 1
        total_folds += 1
print(f"Zero-trade folds: {zero_trade_folds}/{total_folds}")
print(f"Zero-vol folds: {zero_vol_folds}/{total_folds}")
