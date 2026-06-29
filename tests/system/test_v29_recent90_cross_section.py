import importlib.util
import sys
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def _load_module(name: str):
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_assign_quantile_bucket_handles_missing_and_orders_values() -> None:
    module = _load_module("diagnose_v29_recent90_cross_section")
    values = pd.Series({"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0, "e": None})

    buckets = module.assign_quantile_buckets(values, labels=("low", "mid", "high"))

    assert buckets["a"] == "low"
    assert buckets["d"] == "high"
    assert buckets["e"] == "unknown"


def test_group_contribution_summary_sorts_loss_groups_first() -> None:
    module = _load_module("diagnose_v29_recent90_cross_section")
    rows = pd.DataFrame(
        [
            {"industry": "A", "symbol": "000001", "contribution": -0.03, "weight": 0.2},
            {"industry": "A", "symbol": "000002", "contribution": 0.01, "weight": 0.1},
            {"industry": "B", "symbol": "000003", "contribution": -0.005, "weight": 0.2},
        ]
    )

    summary = module.group_contribution_summary(rows, "industry", limit=2)

    assert summary[0]["industry"] == "A"
    assert summary[0]["contribution_sum"] == -0.02
    assert summary[0]["symbol_count"] == 2
    assert summary[0]["avg_weight"] == 0.15


def test_build_candidate_cross_section_report_identifies_may_stock_drag() -> None:
    module = _load_module("diagnose_v29_recent90_cross_section")
    rows = pd.DataFrame(
        [
            {
                "candidate": "candidate_a",
                "sleeve": "price_lowvol_reversal",
                "date": "2026-05-14",
                "symbol": "000001",
                "industry": "半导体",
                "liquidity_bucket": "low",
                "score_bucket": "top",
                "weight": 0.2,
                "stock_return": -0.10,
                "contribution": -0.02,
            },
            {
                "candidate": "candidate_a",
                "sleeve": "price_lowvol_reversal",
                "date": "2026-05-15",
                "symbol": "000002",
                "industry": "医药",
                "liquidity_bucket": "mid",
                "score_bucket": "top",
                "weight": 0.1,
                "stock_return": 0.02,
                "contribution": 0.002,
            },
        ]
    )

    report = module.build_candidate_cross_section_report("candidate_a", rows)

    assert report["candidate"] == "candidate_a"
    assert report["production_ready"] is False
    assert report["not_parameter_tuning"] is True
    assert report["total_raw_stock_contribution"] == -0.018
    assert "not portfolio PnL" in report["raw_unscaled_note"]
    assert report["worst_industries"][0]["industry"] == "半导体"
    assert report["next_required_diagnostics"][0].startswith("Compare")
