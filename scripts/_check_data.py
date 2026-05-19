import pandas as pd, numpy as np, os
data_dir = '/home/hermes/.hermes/projects/lianghua/data/parquet'
files = sorted(os.listdir(data_dir))
lens = []
for f in files[:500]:
    df = pd.read_parquet(os.path.join(data_dir, f))
    lens.append((f, len(df)))
la = np.array([x[1] for x in lens])
print(f"Sample: {len(lens)} files")
print(f"min={la.min()}, max={la.max()}, median={np.median(la):.0f}, mean={la.mean():.0f}")
print(f"<100: {(la<100).sum()}, <252: {(la<252).sum()}, <500: {(la<500).sum()}, <1000: {(la<1000).sum()}")
short = [(f, l) for f, l in lens if l < 100]
print(f"\nFiles with <100 days: {len(short)}")
for f, l in short[:10]:
    print(f"  {f}: {l} days")
