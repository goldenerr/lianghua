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


def test_monthly_bucket_contributions_splits_target_and_history() -> None:
    module = _load_module("diagnose_v29_recent90_historical_contrast")
    rows = pd.DataFrame(
        [
            {"date": "2026-05-06", "industry": "A", "contribution": -0.03},
            {"date": "2026-05-07", "industry": "A", "contribution": -0.01},
            {"date": "2026-04-07", "industry": "A", "contribution": 0.02},
            {"date": "2026-04-08", "industry": "B", "contribution": 0.01},
        ]
    )

    monthly = module.monthly_bucket_contributions(rows, "industry")

    target = monthly[(monthly["month"] == "2026-05") & (monthly["bucket"] == "A")]
    assert float(target.iloc[0]["contribution_sum"]) == -0.04
    assert int(target.iloc[0]["row_count"]) == 2


def test_contrast_bucket_against_positive_months_flags_anomaly() -> None:
    module = _load_module("diagnose_v29_recent90_historical_contrast")
    monthly = pd.DataFrame(
        [
            {"month": "2026-05", "bucket": "A", "contribution_sum": -0.04, "row_count": 2},
            {"month": "2026-04", "bucket": "A", "contribution_sum": 0.02, "row_count": 1},
            {"month": "2026-03", "bucket": "A", "contribution_sum": 0.03, "row_count": 1},
            {"month": "2026-02", "bucket": "A", "contribution_sum": 0.01, "row_count": 1},
        ]
    )
    positive_months = {"2026-02", "2026-03", "2026-04"}

    contrast = module.contrast_bucket_against_positive_months(
        monthly,
        target_month="2026-05",
        bucket="A",
        positive_months=positive_months,
    )

    assert contrast["bucket"] == "A"
    assert contrast["target_contribution"] == -0.04
    assert contrast["positive_month_count"] == 3
    assert contrast["historical_positive_mean"] == 0.02
    assert contrast["direction_flip"] is True
    assert contrast["diagnosis"] == "target_negative_vs_positive_history"


def test_build_candidate_historical_contrast_keeps_fail_closed_flags() -> None:
    module = _load_module("diagnose_v29_recent90_historical_contrast")
    rows = pd.DataFrame(
        [
            {
                "candidate": "c",
                "date": "2026-05-06",
                "industry": "A",
                "liquidity_bucket": "low",
                "score_bucket": "top",
                "sleeve": "s",
                "contribution": -0.03,
            },
            {
                "candidate": "c",
                "date": "2026-04-06",
                "industry": "A",
                "liquidity_bucket": "low",
                "score_bucket": "top",
                "sleeve": "s",
                "contribution": 0.02,
            },
            {
                "candidate": "c",
                "date": "2026-03-06",
                "industry": "A",
                "liquidity_bucket": "low",
                "score_bucket": "top",
                "sleeve": "s",
                "contribution": 0.01,
            },
        ]
    )

    report = module.build_candidate_historical_contrast("c", rows, target_month="2026-05")

    assert report["candidate"] == "c"
    assert report["production_ready"] is False
    assert report["not_parameter_tuning"] is True
    assert report["target_month"] == "2026-05"
    assert report["target_raw_contribution"] == -0.03
    assert report["positive_month_count"] == 2
    assert report["contrasts"]["industry"][0]["direction_flip"] is True
