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
                "date": "2026-05-01",
                "return": -0.02,
                "risk_off": False,
                "bucket_stress_flag": True,
                "triggered_buckets": ["sleeve:a"],
                "contrib_a": -0.01,
                "simulated_return": -0.015,
            },
            {
                "date": "2026-05-02",
                "return": 0.01,
                "risk_off": False,
                "bucket_stress_flag": False,
                "triggered_buckets": [],
                "contrib_a": 0.02,
                "simulated_return": 0.01,
            },
            {
                "date": "2026-05-03",
                "return": -0.01,
                "risk_off": False,
                "bucket_stress_flag": False,
                "triggered_buckets": [],
                "contrib_a": -0.02,
                "simulated_return": -0.01,
            },
            {
                "date": "2026-05-04",
                "return": 0.005,
                "risk_off": True,
                "bucket_stress_flag": False,
                "triggered_buckets": [],
                "contrib_a": -0.03,
                "simulated_return": 0.005,
            },
        ]
    )


def test_randomized_schedule_preserves_flag_count_and_avoids_risk_off_days() -> None:
    module = _load_module("compare_v29_bucket_risk_flag_random_baseline")

    randomized = module.randomized_flag_schedule(_rows(), seed=7)

    assert int(randomized["bucket_stress_flag"].sum()) == 1
    assert randomized.loc[randomized["risk_off"].astype(bool), "bucket_stress_flag"].sum() == 0
    assert all(isinstance(item, list) for item in randomized["triggered_buckets"])


def test_random_baseline_trials_are_deterministic_for_seed() -> None:
    module = _load_module("compare_v29_bucket_risk_flag_random_baseline")

    first = module.run_random_baseline_trials(_rows(), n_trials=10, seed=42, reduction_fraction=0.5)
    second = module.run_random_baseline_trials(
        _rows(), n_trials=10, seed=42, reduction_fraction=0.5
    )

    assert first == second
    assert len(first) == 10
    assert all("simulated_minus_baseline" in item for item in first)


def test_summarize_random_baseline_remains_fail_closed() -> None:
    module = _load_module("compare_v29_bucket_risk_flag_random_baseline")
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
