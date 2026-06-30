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


def _rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "candidate": "c",
                "date": "2026-05-01",
                "return": -0.02,
                "risk_off": False,
                "bucket_aware_flag": True,
                "triggered_bucket_count": 2,
                "triggered_buckets": ["liquidity_bucket:low", "score_bucket:top"],
                "flagged_negative_bucket_contribution": -0.05,
            },
            {
                "candidate": "c",
                "date": "2026-05-02",
                "return": -0.01,
                "risk_off": False,
                "bucket_aware_flag": False,
                "triggered_bucket_count": 0,
                "triggered_buckets": [],
                "flagged_negative_bucket_contribution": 0.0,
            },
            {
                "candidate": "c",
                "date": "2026-05-03",
                "return": 0.01,
                "risk_off": True,
                "bucket_aware_flag": False,
                "triggered_bucket_count": 0,
                "triggered_buckets": [],
                "flagged_negative_bucket_contribution": 0.0,
            },
        ]
    )


def test_randomized_bucket_aware_schedule_preserves_count_and_avoids_risk_off_days() -> None:
    module = _load_module("compare_v29_daily_bucket_aware_random_baseline")

    randomized = module.randomized_bucket_aware_schedule(_rows(), seed=17)

    assert int(randomized["bucket_aware_flag"].sum()) == 1
    assert randomized.loc[randomized["risk_off"].astype(bool), "bucket_aware_flag"].sum() == 0
    flagged = randomized[randomized["bucket_aware_flag"].astype(bool)].iloc[0]
    assert flagged["triggered_bucket_count"] == 2
    assert flagged["triggered_buckets"] == ["liquidity_bucket:low", "score_bucket:top"]
    assert flagged["flagged_negative_bucket_contribution"] == -0.05


def test_bucket_aware_random_trials_are_deterministic() -> None:
    module = _load_module("compare_v29_daily_bucket_aware_random_baseline")

    first = module.run_random_baseline_trials(
        _rows(), n_trials=12, seed=42, reduction_fraction=0.5, max_daily_delta_fraction=0.25
    )
    second = module.run_random_baseline_trials(
        _rows(), n_trials=12, seed=42, reduction_fraction=0.5, max_daily_delta_fraction=0.25
    )

    assert first == second
    assert len(first) == 12
    assert all("simulated_minus_baseline" in item for item in first)


def test_summarize_bucket_aware_random_baseline_is_fail_closed() -> None:
    module = _load_module("compare_v29_daily_bucket_aware_random_baseline")
    observed = {"candidate": "c", "simulated_minus_baseline": 0.0001, "flagged_day_count": 1}
    trials = [
        {"simulated_minus_baseline": -0.0001},
        {"simulated_minus_baseline": 0.0002},
        {"simulated_minus_baseline": 0.0003},
    ]

    summary = module.summarize_random_baseline("c", observed, trials)

    assert summary["candidate"] == "c"
    assert summary["production_ready"] is False
    assert summary["not_parameter_tuning"] is True
    assert summary["can_change_strategy_now"] is False
    assert summary["beats_random_median"] is False
    assert summary["observed_delta"] == 0.0001
    assert summary["diagnosis"] == "does_not_beat_random_median"
