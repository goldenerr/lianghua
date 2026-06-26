import importlib.util
import sys
from datetime import datetime, timezone
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


def _sample_summary():
    return {
        "candidate": "candidate_a",
        "recent_days": 90,
        "recent_metrics": {"total_return": -0.035, "sharpe_ratio": -2.4, "max_drawdown": -0.04},
        "avg_stock_exposure_recent": 0.25,
        "risk_off_ratio_recent": 0.35,
        "mdd_state_counts_recent": {"normal": 72, "reduce_scale": 18},
        "contribution_totals_recent": {
            "contrib_price_lowvol_reversal": -0.013,
            "contrib_price_ic_diversified": -0.009,
            "contrib_price_defensive_breadth": -0.007,
            "crisis_contrib": 0.001,
            "carry_contrib": 0.0,
        },
        "monthly_recent": [
            {
                "month": "2026-04",
                "return": 0.001,
                "risk_off_ratio": 0.4,
                "avg_stock_exposure": 0.2,
                "cost_sum": 0.0001,
            },
            {
                "month": "2026-05",
                "return": -0.027,
                "risk_off_ratio": 0.0,
                "avg_stock_exposure": 0.46,
                "cost_sum": 0.0004,
            },
        ],
    }


def test_classifies_stock_decay_and_regime_lag_without_tuning() -> None:
    module = _load_module("classify_v29_recent90_failure_modes")

    result = module.classify_candidate(_sample_summary())

    assert "stock_alpha_sleeve_decay" in result["failure_modes"]
    assert "defensive_sleeve_under_offset" in result["failure_modes"]
    assert "regime_detection_lag_or_under_de_risking" in result["failure_modes"]
    assert "drawdown_scaling_active_after_damage" in result["failure_modes"]
    assert result["production_ready"] is False
    assert result["not_parameter_tuning"] is True


def test_build_report_keeps_industry_claim_and_tuning_closed() -> None:
    module = _load_module("classify_v29_recent90_failure_modes")

    report = module.build_report(
        {"version": "input", "summaries": [_sample_summary()]},
        datetime(2026, 6, 26, tzinfo=timezone.utc),
    )

    assert report["production_ready"] is False
    assert report["decision"]["can_claim_industry_leading"] is False
    assert report["decision"]["should_parameter_tune_now"] is False
    assert report["candidate_count"] == 1
    assert "stock_alpha_sleeve_decay" in report["global_failure_modes"]


def test_positive_recent_window_is_not_misclassified_as_negative() -> None:
    module = _load_module("classify_v29_recent90_failure_modes")
    summary = _sample_summary()
    summary["recent_metrics"] = {"total_return": 0.01, "sharpe_ratio": 1.0, "max_drawdown": -0.01}
    summary["contribution_totals_recent"] = {"contrib_price_a": 0.01, "crisis_contrib": 0.0}

    result = module.classify_candidate(summary)

    assert result["failure_modes"] == ["recent_window_not_negative"]
