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


def test_make_walk_forward_folds_are_chronological_and_train_before_oos() -> None:
    module = _load_module("validate_v29_bucket_aware_wf")
    dates = pd.date_range("2020-01-01", periods=30, freq="D")

    folds = module.make_walk_forward_folds(dates, n_folds=2, train_days=10, test_days=5)

    assert len(folds) == 2
    assert folds[0]["train_end"] < folds[0]["test_start"]
    assert folds[0]["test_end"] < folds[1]["test_start"]
    assert folds[0]["train_n_days"] == 10
    assert folds[0]["test_n_days"] == 5


def test_train_specs_use_training_months_only_and_exclude_oos_dates() -> None:
    module = _load_module("validate_v29_bucket_aware_wf")
    bucket_state = pd.DataFrame(
        [
            {
                "candidate": "c",
                "date": "2020-01-02",
                "bucket_type": "score_bucket",
                "bucket": "top",
                "contribution_sum": -0.03,
            },
            {
                "candidate": "c",
                "date": "2020-01-03",
                "bucket_type": "score_bucket",
                "bucket": "top",
                "contribution_sum": -0.02,
            },
            {
                "candidate": "c",
                "date": "2020-02-02",
                "bucket_type": "score_bucket",
                "bucket": "top",
                "contribution_sum": 0.04,
            },
            {
                "candidate": "c",
                "date": "2020-03-02",
                "bucket_type": "score_bucket",
                "bucket": "top",
                "contribution_sum": -0.50,
            },
            {
                "candidate": "c",
                "date": "2020-01-02",
                "bucket_type": "sleeve",
                "bucket": "ignored",
                "contribution_sum": -1.0,
            },
        ]
    )
    portfolio_rows = pd.DataFrame(
        [
            {"candidate": "c", "date": "2020-01-02", "return": -0.01},
            {"candidate": "c", "date": "2020-01-03", "return": -0.01},
            {"candidate": "c", "date": "2020-02-02", "return": 0.02},
            {"candidate": "c", "date": "2020-03-02", "return": -0.30},
        ]
    )

    specs = module.build_train_bucket_trigger_specs(
        bucket_state,
        portfolio_rows,
        candidate="c",
        train_start=pd.Timestamp("2020-01-01"),
        train_end=pd.Timestamp("2020-02-29"),
        observation_days=3,
        max_specs_per_type=3,
    )

    assert len(specs) == 1
    assert specs[0]["bucket_type"] == "score_bucket"
    assert specs[0]["bucket"] == "top"
    assert specs[0]["train_start"] == "2020-01-01"
    assert specs[0]["train_end"] == "2020-02-29"
    assert specs[0]["oos_data_used"] is False
    assert specs[0]["train_negative_month_count"] == 1
    assert specs[0]["daily_trigger_threshold"] < 0
    assert "sleeve" not in {spec["bucket_type"] for spec in specs}


def test_summarize_fold_validation_remains_research_only() -> None:
    module = _load_module("validate_v29_bucket_aware_wf")
    fold_rows = pd.DataFrame(
        [
            {
                "candidate": "c",
                "date": "2020-03-01",
                "return": -0.01,
                "simulated_return": -0.0075,
                "bucket_aware_flag": True,
                "bucket_aware_delta": 0.0025,
            },
            {
                "candidate": "c",
                "date": "2020-03-02",
                "return": 0.01,
                "simulated_return": 0.01,
                "bucket_aware_flag": False,
                "bucket_aware_delta": 0.0,
            },
        ]
    )
    random_trials = [{"simulated_minus_baseline": 0.003}, {"simulated_minus_baseline": 0.004}]

    summary = module.summarize_wf_fold("c", {"fold": 0}, fold_rows, random_trials)

    assert summary["production_ready"] is False
    assert summary["can_change_strategy_now"] is False
    assert summary["observed_delta"] > 0
    assert summary["random_trial_count"] == 2
    assert summary["beats_random_median"] is False
