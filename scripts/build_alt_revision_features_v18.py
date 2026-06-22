#!/usr/bin/env python3
"""Build V18 stock alternative features with rolling analyst-revision signals."""

from __future__ import annotations

import json
import os
from datetime import datetime

import pandas as pd
import validate_quant_logic_v5_9 as base
from _paths import ALT_FEATURES_DIR
from analyze_alt_factor_ic_v18 import _load_base_alt_features, _load_rolling_analyst_features

ALT_FEATURES_DIR.mkdir(parents=True, exist_ok=True)


def _write_atomically(path: os.PathLike[str] | str, df: pd.DataFrame) -> None:
    out_path = ALT_FEATURES_DIR / os.fspath(path)
    tmp_path = out_path.with_name(f"{out_path.name}.{os.getpid()}.tmp")
    try:
        df.to_parquet(tmp_path)
        tmp_path.replace(out_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def main() -> None:
    close, _volume, _amount, _turnover = base._load_aligned_data()
    base_features = _load_base_alt_features(close.columns)
    rolling_features = _load_rolling_analyst_features(close.columns, close.index)
    if base_features.empty:
        combined = rolling_features
    elif rolling_features.empty:
        combined = base_features
    else:
        combined = base_features.merge(rolling_features, on=["date", "code"], how="outer")
    combined = combined.sort_values(["date", "code"]).reset_index(drop=True)
    _write_atomically("v18_stock_alt_features.parquet", combined)
    summary = {
        "timestamp": datetime.now().isoformat(),
        "source": [
            "v17_stock_alt_features",
            "cninfo_analyst_ratings_rolling_20d_60d",
        ],
        "rows": len(combined),
        "symbols": int(combined["code"].nunique()) if not combined.empty else 0,
        "dates": int(combined["date"].nunique()) if not combined.empty else 0,
        "rolling_columns": [
            column
            for column in combined.columns
            if column.startswith("analyst_") and column.endswith(("20d", "60d"))
        ],
        "pit_warning": (
            "Rolling analyst features use publish_date history only. Signals must still be lagged "
            "by the research/backtest layer before trading."
        ),
    }
    (ALT_FEATURES_DIR / "v18_alt_feature_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(
        f"Built V18 alt features: rows={summary['rows']} symbols={summary['symbols']} "
        f"dates={summary['dates']}",
        flush=True,
    )


if __name__ == "__main__":
    main()
