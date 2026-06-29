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


def test_score_bucket_stress_prioritizes_large_direction_flips() -> None:
    module = _load_module("plan_v29_bucket_risk_flag_hypothesis")
    item = {
        "bucket": "price_lowvol_reversal",
        "target_contribution": -0.053975,
        "historical_positive_mean": 0.048655,
        "historical_positive_hit_rate": 0.8,
        "direction_flip": True,
    }

    scored = module.score_bucket_stress("sleeve", item)

    assert scored["bucket_type"] == "sleeve"
    assert scored["bucket"] == "price_lowvol_reversal"
    assert scored["stress_score"] > 0.08
    assert scored["eligible_for_flag_hypothesis"] is True


def test_build_flag_hypothesis_keeps_fail_closed_and_requires_validation() -> None:
    module = _load_module("plan_v29_bucket_risk_flag_hypothesis")
    evidence = {
        "candidate": "v29_price_meta_longhorizon_guard",
        "diagnosis": "failed_buckets_flip_vs_positive_history",
        "contrasts": {
            "sleeve": [
                {
                    "bucket": "price_lowvol_reversal",
                    "target_contribution": -0.053975,
                    "historical_positive_mean": 0.048655,
                    "historical_positive_hit_rate": 0.8,
                    "direction_flip": True,
                }
            ],
            "industry": [],
            "liquidity_bucket": [],
            "score_bucket": [],
        },
    }
    timing = {
        "candidate": "v29_price_meta_longhorizon_guard",
        "target_month": {"diagnosis": "risk_off_missed_loss_window", "risk_off_ratio": 0.0},
    }

    hypothesis = module.build_candidate_hypothesis(evidence, timing)

    assert hypothesis["candidate"] == "v29_price_meta_longhorizon_guard"
    assert hypothesis["production_ready"] is False
    assert hypothesis["not_parameter_tuning"] is True
    assert hypothesis["hypothesis_type"] == "sleeve_bucket_stress_flag"
    assert hypothesis["can_implement_strategy_change_now"] is False
    assert "walk_forward" in hypothesis["required_validation"][0]


def test_rank_flag_inputs_returns_top_stress_items() -> None:
    module = _load_module("plan_v29_bucket_risk_flag_hypothesis")
    contrasts = {
        "sleeve": [
            {
                "bucket": "a",
                "target_contribution": -0.05,
                "historical_positive_mean": 0.05,
                "historical_positive_hit_rate": 0.9,
                "direction_flip": True,
            },
            {
                "bucket": "b",
                "target_contribution": -0.01,
                "historical_positive_mean": 0.01,
                "historical_positive_hit_rate": 0.9,
                "direction_flip": True,
            },
        ],
        "industry": [
            {
                "bucket": "c",
                "target_contribution": -0.08,
                "historical_positive_mean": 0.02,
                "historical_positive_hit_rate": 0.7,
                "direction_flip": True,
            }
        ],
    }

    ranked = module.rank_flag_inputs(contrasts, limit=2)

    assert len(ranked) == 2
    assert ranked[0]["bucket"] in {"a", "c"}
    assert ranked[0]["stress_score"] >= ranked[1]["stress_score"]
