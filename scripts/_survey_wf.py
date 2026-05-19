import pandas as pd, numpy as np
from pathlib import Path

DATA_DIR = Path("/home/hermes/.hermes/projects/lianghua/data/parquet")

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
    if len(strat_rets) < 50: return None
    ann_ret = np.mean(strat_rets) * 252
    ann_vol = np.std(strat_rets, ddof=1) * np.sqrt(252)
    sharpe = (ann_ret - 0.02) / max(ann_vol, 1e-10)
    return {'sharpe': sharpe, 'ann_ret': ann_ret, 'ann_vol': ann_vol}

def strat(p): return sma_crossover(p, 10, 30)

# Survey 300 stocks, find extreme WF Sharpes
oos_sharpes = []
extreme = []
files = sorted(DATA_DIR.glob("*.parquet"))[:300]
for f in files:
    df = pd.read_parquet(f)
    prices = df['close'].values
    n = len(prices)
    if n < 252: continue
    oos_size = int(n * 0.20)
    fold_size = (n - oos_size) // 5
    fold_oos = []
    for fold in range(5):
        train_end = oos_size + fold * fold_size
        test_end = min(train_end + fold_size, n)
        test_prices = prices[train_end:test_end]
        bt = backtest(test_prices, strat)
        if bt:
            fold_oos.append(bt['sharpe'])
    if fold_oos:
        avg = np.mean(fold_oos)
        oos_sharpes.append(avg)
        if abs(avg) > 100:
            extreme.append((f.stem, avg, fold_oos))

print(f"Surveyed {len(files)}, valid WF: {len(oos_sharpes)}")
oa = np.array(oos_sharpes)
print(f"OOS Sharpe: mean={oa.mean():.2f} median={np.median(oa):.2f} min={oa.min():.2f} max={oa.max():.2f}")
print(f"abs>100: {(abs(oa)>100).sum()}")
print(f"\nExtreme Sharpe stocks (|oos|>100):")
for sym, avg, folds in extreme[:10]:
    print(f"  {sym}: avg_oos={avg:.1f}, folds={[round(x,1) for x in folds]}")
