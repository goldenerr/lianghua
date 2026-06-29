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


def test_lagged_bucket_stress_flag_uses_prior_rows_only() -> None:
    module = _load_module("simulate_v29_bucket_risk_flag")
    rows = pd.DataFrame(
        [
            {"date": "2026-05-06", "risk_off": False, "contrib_price_lowvol_reversal": -0.04},
            {"date": "2026-05-07", "risk_off": False, "contrib_price_lowvol_reversal": 0.01},
        ]
    )
    inputs = [
        {
            "bucket_type": "sleeve",
            "bucket": "price_lowvol_reversal",
            "daily_trigger_threshold": -0.02,
        }
    ]

    flagged = module.apply_lagged_bucket_stress_flags(rows, inputs, observation_days=1)

    assert flagged.loc[0, "bucket_stress_flag"] is False
    assert flagged.loc[1, "bucket_stress_flag"] is True
    assert flagged.loc[1, "triggered_buckets"] == ["sleeve:price_lowvol_reversal"]


def test_simulate_research_gate_reduces_only_flagged_sleeve_contribution() -> None:
    module = _load_module("simulate_v29_bucket_risk_flag")
    rows = pd.DataFrame(
        [
            {
                "date": "2026-05-07",
                "return": -0.03,
                "bucket_stress_flag": True,
                "contrib_price_lowvol_reversal": -0.02,
                "triggered_buckets": ["sleeve:price_lowvol_reversal"],
            },
            {
                "date": "2026-05-08",
                "return": 0.01,
                "bucket_stress_flag": False,
                "contrib_price_lowvol_reversal": 0.02,
                "triggered_buckets": [],
            },
        ]
    )

    simulated = module.simulate_research_gate(rows, reduction_fraction=0.5)

    assert simulated.loc[0, "simulated_return"] == -0.02
    assert simulated.loc[0, "avoided_loss_contribution"] == 0.01
    assert simulated.loc[1, "simulated_return"] == 0.01
    assert simulated.loc[1, "avoided_loss_contribution"] == 0.0


def test_summarize_candidate_simulation_remains_fail_closed() -> None:
    module = _load_module("simulate_v29_bucket_risk_flag")
    rows = pd.DataFrame(
        [
            {
                "date": "2026-05-07",
                "return": -0.03,
                "simulated_return": -0.02,
                "bucket_stress_flag": True,
            },
            {
                "date": "2026-05-08",
                "return": 0.01,
                "simulated_return": 0.01,
                "bucket_stress_flag": False,
            },
        ]
    )

    summary = module.summarize_candidate_simulation("candidate", rows)

    assert summary["candidate"] == "candidate"
    assert summary["production_ready"] is False
    assert summary["not_parameter_tuning"] is True
    assert summary["can_change_strategy_now"] is False
    assert summary["flagged_day_count"] == 1
    assert summary["baseline_compound_return"] < summary["simulated_compound_return"]
