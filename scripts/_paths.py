"""Environment-configurable paths shared by research and validation scripts."""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_DIR = Path(os.getenv("QUANT_PROJECT_DIR", Path(__file__).resolve().parents[1]))
DATA_DIR = Path(os.getenv("QUANT_DATA_DIR", PROJECT_DIR / "data" / "parquet"))
RESULTS_DIR = Path(os.getenv("QUANT_RESULTS_DIR", PROJECT_DIR / "data" / "backtest_results"))
FUNDAMENTALS_DIR = Path(os.getenv("QUANT_FUNDAMENTALS_DIR", PROJECT_DIR / "data" / "fundamentals"))
STOCK_LIST = Path(os.getenv("QUANT_STOCK_LIST", PROJECT_DIR / "data" / "stock_list.json"))
INDUSTRY_PATH = Path(os.getenv("QUANT_INDUSTRY_PATH", PROJECT_DIR / "data" / "industry_fixed.parquet"))
