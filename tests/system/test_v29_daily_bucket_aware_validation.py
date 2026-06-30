import importlib.util
import sys
from pathlib import Path

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


def test_lookahead_audit_flags_posthoc_hypothesis_source() -> None:
    module = _load_module("validate_v29_daily_bucket_aware_flag")
    simulation = {
        "target_month": "2026-05",
        "simulation_parameters": {"lagged_inputs_only": True, "max_daily_delta_fraction": 0.25},
        "simulated_daily_records": [
            {
                "candidate": "c",
                "date": "2026-05-01",
                "return": -0.01,
                "bucket_aware_flag": True,
                "bucket_aware_delta": 0.002,
            }
        ],
    }
    hypothesis = {"source_reports": {"target_month": "2026-05"}}

    audit = module.audit_lookahead(simulation, hypothesis)

    assert audit["lagged_mechanics_pass"] is True
    assert audit["posthoc_hypothesis_source_blocker"] is True
    assert audit["passes"] is False


def test_monthly_distribution_requires_multiple_months() -> None:
    module = _load_module("validate_v29_daily_bucket_aware_flag")
    simulation = {
        "simulated_daily_records": [
            {"candidate": "c", "date": "2026-05-01", "return": -0.01, "simulated_return": -0.008},
            {"candidate": "c", "date": "2026-05-02", "return": 0.01, "simulated_return": 0.01},
        ]
    }

    monthly = module.monthly_distribution_check(simulation)

    assert monthly["passes"] is False
    assert monthly["unique_month_count"] == 1
    assert "insufficient_month_coverage" in monthly["blockers"]


def test_wf_validation_requires_fold_records_and_stays_fail_closed() -> None:
    module = _load_module("validate_v29_daily_bucket_aware_flag")
    simulation = {"summaries": [{"candidate": "c", "simulated_minus_baseline": 0.1}]}
    random_baseline = {
        "summaries": [
            {
                "candidate": "c",
                "beats_random_median": True,
                "diagnosis": "beats_random_median_only_not_validated",
            }
        ]
    }

    wf = module.walk_forward_validation_check(simulation, random_baseline)

    assert wf["passes"] is False
    assert wf["fold_count"] == 0
    assert "no_walk_forward_fold_records" in wf["blockers"]
