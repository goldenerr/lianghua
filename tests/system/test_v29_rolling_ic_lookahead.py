import importlib.util
import sys
from pathlib import Path

import numpy as np
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


def test_rolling_ic_excludes_labels_not_observable_after_five_day_horizon() -> None:
    module = _load_module("research_v28_factor_direction")
    dates = pd.date_range("2020-01-01", periods=120, freq="B")
    symbols = [f"{symbol:06d}" for symbol in range(100)]
    rng = np.random.default_rng(42)
    factor = pd.DataFrame(rng.normal(size=(len(dates), len(symbols))), index=dates, columns=symbols)
    future_5d = pd.DataFrame(rng.normal(size=(len(dates), len(symbols))), index=dates, columns=symbols)
    decision_idx = 100

    original = module._rolling_ic_weights({"factor": factor}, future_5d, lookback=80)
    revised_future = future_5d.copy()
    revised_future.iloc[decision_idx - 4 : decision_idx + 1] *= -100.0
    revised = module._rolling_ic_weights({"factor": factor}, revised_future, lookback=80)

    pd.testing.assert_series_equal(
        original.iloc[decision_idx],
        revised.iloc[decision_idx],
        check_names=False,
    )
