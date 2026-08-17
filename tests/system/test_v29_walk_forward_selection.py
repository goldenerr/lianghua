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


def test_walk_forward_selects_on_train_and_freezes_candidate_for_oos() -> None:
    module = _load_module("research_v29_portfolio_layer")
    assert hasattr(module, "_walk_forward_select_candidates")
    dates = pd.date_range("2020-01-01", periods=20, freq="B")
    train_slice = slice(5, 15)
    test_slice = slice(15, 20)
    candidate_a = pd.Series(0.0, index=dates, dtype=float)
    candidate_b = pd.Series(0.0, index=dates, dtype=float)
    candidate_a.iloc[train_slice] = [0.010, 0.012, 0.008, 0.011, 0.009] * 2
    candidate_b.iloc[train_slice] = [0.001, -0.001, 0.002, -0.001, 0.001] * 2
    candidate_a.iloc[test_slice] = [-0.010, -0.020, -0.005, -0.015, -0.010]
    candidate_b.iloc[test_slice] = [0.20, 0.18, 0.22, 0.19, 0.21]

    result = module._walk_forward_select_candidates(
        {"candidate_a": candidate_a, "candidate_b": candidate_b},
        production_comparable={"candidate_a": True, "candidate_b": True},
        n_folds=1,
        train_days=10,
        test_days=5,
    )

    assert result["valid"] is True
    assert result["selection_mode"] == "train_only_candidate_selection"
    assert result["oos_data_used_for_selection"] is False
    assert result["folds"][0]["selected_candidate"] == "candidate_a"
    assert result["folds"][0]["train_end"] < result["folds"][0]["test_start"]
    assert result["folds"][0]["oos"] < 0.0


def test_walk_forward_selection_is_invariant_to_unselected_oos_mutation() -> None:
    module = _load_module("research_v29_portfolio_layer")
    assert hasattr(module, "_walk_forward_select_candidates")
    dates = pd.date_range("2020-01-01", periods=20, freq="B")
    candidate_a = pd.Series(0.001, index=dates, dtype=float)
    candidate_b = pd.Series(0.0, index=dates, dtype=float)
    candidate_a.iloc[5:15] = [0.010, 0.012, 0.008, 0.011, 0.009] * 2
    candidate_b.iloc[5:15] = [0.001, -0.001, 0.002, -0.001, 0.001] * 2

    baseline = module._walk_forward_select_candidates(
        {"candidate_a": candidate_a, "candidate_b": candidate_b},
        production_comparable={"candidate_a": True, "candidate_b": True},
        n_folds=1,
        train_days=10,
        test_days=5,
    )
    candidate_b.iloc[15:20] = 100.0
    mutated = module._walk_forward_select_candidates(
        {"candidate_a": candidate_a, "candidate_b": candidate_b},
        production_comparable={"candidate_a": True, "candidate_b": True},
        n_folds=1,
        train_days=10,
        test_days=5,
    )

    assert baseline["folds"][0]["selected_candidate"] == "candidate_a"
    assert mutated["folds"][0]["selected_candidate"] == "candidate_a"
    assert mutated["folds"][0]["train_candidate_metrics"] == baseline["folds"][0]["train_candidate_metrics"]
    assert mutated["folds"][0]["oos"] == baseline["folds"][0]["oos"]


def test_walk_forward_fails_closed_when_requested_folds_are_incomplete() -> None:
    module = _load_module("research_v29_portfolio_layer")
    dates = pd.date_range("2020-01-01", periods=20, freq="B")
    returns = pd.Series([0.001, -0.001] * 10, index=dates, dtype=float)

    result = module._walk_forward_select_candidates(
        {"candidate_a": returns},
        production_comparable={"candidate_a": True},
        n_folds=5,
        train_days=10,
        test_days=5,
    )

    assert result["valid"] is False
    assert result["invalid_reason"] == "insufficient aligned history for all requested folds"
    assert result["folds"] == []
