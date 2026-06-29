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


def test_build_date_level_bucket_flags_prefers_liquidity_and_score_without_double_count() -> None:
    module = _load_module("simulate_v29_daily_bucket_aware_flag")
    state = pd.DataFrame(
        [
            {
                "candidate": "c",
                "date": "2026-05-06",
                "bucket_type": "liquidity_bucket",
                "bucket": "low",
                "contribution_sum": -0.03,
                "bucket_stress_flag": True,
            },
            {
                "candidate": "c",
                "date": "2026-05-06",
                "bucket_type": "score_bucket",
                "bucket": "top",
                "contribution_sum": -0.02,
                "bucket_stress_flag": True,
            },
            {
                "candidate": "c",
                "date": "2026-05-06",
                "bucket_type": "sleeve",
                "bucket": "s",
                "contribution_sum": -0.05,
                "bucket_stress_flag": True,
            },
        ]
    )

    flags = module.build_date_level_bucket_flags(state)

    assert len(flags) == 1
    row = flags.iloc[0]
    assert row["candidate"] == "c"
    assert row["date"] == "2026-05-06"
    assert row["bucket_aware_flag"] is True
    assert row["triggered_bucket_count"] == 2
    assert row["flagged_negative_bucket_contribution"] == -0.05
    assert "sleeve:s" not in row["triggered_buckets"]


def test_simulate_bucket_aware_gate_uses_lagged_flag_and_caps_delta() -> None:
    module = _load_module("simulate_v29_daily_bucket_aware_flag")
    portfolio = pd.DataFrame(
        [
            {"candidate": "c", "date": "2026-05-06", "return": -0.01},
            {"candidate": "c", "date": "2026-05-07", "return": 0.01},
        ]
    )
    flags = pd.DataFrame(
        [
            {
                "candidate": "c",
                "date": "2026-05-06",
                "bucket_aware_flag": True,
                "flagged_negative_bucket_contribution": -1.0,
                "triggered_buckets": ["score_bucket:top"],
            },
            {
                "candidate": "c",
                "date": "2026-05-07",
                "bucket_aware_flag": False,
                "flagged_negative_bucket_contribution": 0.0,
                "triggered_buckets": [],
            },
        ]
    )

    simulated = module.simulate_bucket_aware_gate(
        portfolio, flags, reduction_fraction=0.5, max_daily_delta_fraction=0.25
    )

    assert simulated.loc[0, "simulated_return"] == -0.0075
    assert simulated.loc[0, "bucket_aware_delta"] == 0.0025
    assert simulated.loc[1, "simulated_return"] == 0.01
    assert simulated.loc[1, "bucket_aware_delta"] == 0.0


def test_summarize_bucket_aware_simulation_remains_fail_closed() -> None:
    module = _load_module("simulate_v29_daily_bucket_aware_flag")
    rows = pd.DataFrame(
        [
            {
                "candidate": "c",
                "date": "2026-05-06",
                "return": -0.01,
                "simulated_return": -0.0075,
                "bucket_aware_flag": True,
                "bucket_aware_delta": 0.0025,
            },
            {
                "candidate": "c",
                "date": "2026-05-07",
                "return": 0.01,
                "simulated_return": 0.01,
                "bucket_aware_flag": False,
                "bucket_aware_delta": 0.0,
            },
        ]
    )

    summary = module.summarize_bucket_aware_simulation("c", rows)

    assert summary["candidate"] == "c"
    assert summary["production_ready"] is False
    assert summary["not_parameter_tuning"] is True
    assert summary["can_change_strategy_now"] is False
    assert summary["flagged_day_count"] == 1
    assert summary["simulated_minus_baseline"] > 0
