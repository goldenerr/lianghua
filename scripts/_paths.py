"""Environment-configurable paths shared by research and validation scripts."""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_DIR = Path(os.getenv("QUANT_PROJECT_DIR", Path(__file__).resolve().parents[1]))
DATA_DIR = Path(os.getenv("QUANT_DATA_DIR", PROJECT_DIR / "data" / "parquet"))
RESULTS_DIR = Path(os.getenv("QUANT_RESULTS_DIR", PROJECT_DIR / "data" / "backtest_results"))
FUNDAMENTALS_DIR = Path(os.getenv("QUANT_FUNDAMENTALS_DIR", PROJECT_DIR / "data" / "fundamentals"))
EARNINGS_EVENTS_DIR = Path(
    os.getenv("QUANT_EARNINGS_EVENTS_DIR", PROJECT_DIR / "data" / "earnings_events")
)
ANALYST_EXPECTATIONS_DIR = Path(
    os.getenv("QUANT_ANALYST_EXPECTATIONS_DIR", PROJECT_DIR / "data" / "analyst_expectations")
)
FLOW_SIGNALS_DIR = Path(os.getenv("QUANT_FLOW_SIGNALS_DIR", PROJECT_DIR / "data" / "flow_signals"))
INTRADAY_DIR = Path(os.getenv("QUANT_INTRADAY_DIR", PROJECT_DIR / "data" / "intraday"))
HEDGE_ASSETS_DIR = Path(os.getenv("QUANT_HEDGE_ASSETS_DIR", PROJECT_DIR / "data" / "hedge_assets"))
ALT_FEATURES_DIR = Path(os.getenv("QUANT_ALT_FEATURES_DIR", PROJECT_DIR / "data" / "alt_features"))
ALT_ARCHIVES_DIR = Path(os.getenv("QUANT_ALT_ARCHIVES_DIR", PROJECT_DIR / "data" / "alt_archives"))
STOCK_LIST = Path(os.getenv("QUANT_STOCK_LIST", PROJECT_DIR / "data" / "stock_list.json"))
INDUSTRY_PATH = Path(os.getenv("QUANT_INDUSTRY_PATH", PROJECT_DIR / "data" / "industry_fixed.parquet"))
