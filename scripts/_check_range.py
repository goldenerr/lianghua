import pandas as pd, numpy as np, os
from _paths import DATA_DIR

data_dir = str(DATA_DIR)
lens = []
for f in os.listdir(data_dir):
    if not f.endswith('.parquet'): continue
    df = pd.read_parquet(os.path.join(data_dir, f))
    lens.append(len(df))
la = np.array(lens)
print(f"N={len(la)}, min={la.min()}, p10={np.percentile(la,10):.0f}, p25={np.percentile(la,25):.0f}")
print(f"p50={np.median(la):.0f}, p75={np.percentile(la,75):.0f}, p90={np.percentile(la,90):.0f}, max={la.max()}")
print(f">6000: {(la>6000).sum()}, >6200: {(la>6200).sum()}, >6300: {(la>6300).sum()}")
print(f"<3000: {(la<3000).sum()}, <1000: {(la<1000).sum()}, <500: {(la<500).sum()}")
