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


def test_aggregate_daily_bucket_state_groups_liquidity_and_score() -> None:
    module = _load_module("diagnose_v29_recent90_daily_bucket_state")
    rows = pd.DataFrame(
        [
            {
                "candidate": "c",
                "date": "2026-05-06",
                "liquidity_bucket": "low",
                "score_bucket": "top",
                "contribution": -0.01,
                "symbol": "A",
            },
            {
                "candidate": "c",
                "date": "2026-05-06",
                "liquidity_bucket": "low",
                "score_bucket": "mid",
                "contribution": -0.02,
                "symbol": "B",
            },
            {
                "candidate": "c",
                "date": "2026-05-07",
                "liquidity_bucket": "high",
                "score_bucket": "top",
                "contribution": 0.03,
                "symbol": "A",
            },
        ]
    )

    state = module.aggregate_daily_bucket_state(
        rows, bucket_columns=("liquidity_bucket", "score_bucket")
    )

    low = state[
        (state["date"] == "2026-05-06")
        & (state["bucket_type"] == "liquidity_bucket")
        & (state["bucket"] == "low")
    ]
    assert float(low.iloc[0]["contribution_sum"]) == -0.03
    assert int(low.iloc[0]["row_count"]) == 2
    top = state[
        (state["date"] == "2026-05-06")
        & (state["bucket_type"] == "score_bucket")
        & (state["bucket"] == "top")
    ]
    assert float(top.iloc[0]["contribution_sum"]) == -0.01


def test_add_lagged_stress_uses_prior_days_only() -> None:
    module = _load_module("diagnose_v29_recent90_daily_bucket_state")
    state = pd.DataFrame(
        [
            {
                "candidate": "c",
                "date": "2026-05-06",
                "bucket_type": "liquidity_bucket",
                "bucket": "low",
                "contribution_sum": -0.03,
            },
            {
                "candidate": "c",
                "date": "2026-05-07",
                "bucket_type": "liquidity_bucket",
                "bucket": "low",
                "contribution_sum": 0.01,
            },
        ]
    )
    specs = [{"bucket_type": "liquidity_bucket", "bucket": "low", "daily_trigger_threshold": -0.02}]

    flagged = module.add_lagged_stress_state(state, specs, observation_days=1)

    assert flagged.loc[0, "bucket_stress_flag"] is False
    assert flagged.loc[1, "bucket_stress_flag"] is True
    assert flagged.loc[1, "rolling_prior_contribution"] == -0.03


def test_summarize_daily_bucket_state_remains_research_only() -> None:
    module = _load_module("diagnose_v29_recent90_daily_bucket_state")
    flagged = pd.DataFrame(
        [
            {
                "candidate": "c",
                "date": "2026-05-06",
                "bucket_type": "liquidity_bucket",
                "bucket": "low",
                "contribution_sum": -0.03,
                "bucket_stress_flag": False,
            },
            {
                "candidate": "c",
                "date": "2026-05-07",
                "bucket_type": "liquidity_bucket",
                "bucket": "low",
                "contribution_sum": 0.01,
                "bucket_stress_flag": True,
            },
        ]
    )

    summary = module.summarize_candidate_bucket_state("c", flagged)

    assert summary["candidate"] == "c"
    assert summary["production_ready"] is False
    assert summary["not_parameter_tuning"] is True
    assert summary["can_change_strategy_now"] is False
    assert summary["flagged_bucket_day_count"] == 1
