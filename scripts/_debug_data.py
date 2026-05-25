import pandas as pd, numpy as np
from pathlib import Path

from _paths import DATA_DIR
files = sorted(DATA_DIR.glob("*.parquet"))[:3]

for f in files:
    df = pd.read_parquet(f)
    print(f"\n=== {f.stem} ===")
    print(f"Columns: {list(df.columns)}")
    print(f"Shape: {df.shape}")
    
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        print(f"Date type: {type(df['date'].iloc[0])}")
        print(f"Date range: {df['date'].iloc[0]} → {df['date'].iloc[-1]}")
        # Check if 'close' is a column
        if "close" in df.columns:
            print(f"Close: {df['close'].iloc[0]:.2f} → {df['close'].iloc[-1]:.2f}")
    else:
        print("No 'date' column!")
        print(f"First 5 index: {df.index[:5].tolist()}")
