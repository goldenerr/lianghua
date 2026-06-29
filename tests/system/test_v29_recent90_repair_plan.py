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


def _sample_regime():
    return {
        "version": "regime",
        "summaries": [
            {
                "candidate": "candidate_a",
                "recent_days": 90,
                "recent_metrics": {
                    "sharpe_ratio": -2.4,
                    "total_return": -0.035,
                    "max_drawdown": -0.045,
                },
                "avg_stock_exposure_recent": 0.25,
                "mdd_state_counts_recent": {"normal": 72, "reduce_scale": 18},
                "contribution_totals_recent": {
                    "contrib_price_lowvol_reversal": -0.013,
                    "contrib_price_ic_diversified": -0.009,
                    "crisis_contrib": 0.001,
                    "carry_contrib": 0.0,
                },
                "monthly_recent": [
                    {
                        "month": "2026-04",
                        "return": 0.001,
                        "avg_stock_exposure": 0.2,
                        "risk_off_ratio": 0.4,
                    },
                    {
                        "month": "2026-05",
                        "return": -0.027,
                        "avg_stock_exposure": 0.46,
                        "risk_off_ratio": 0.0,
                    },
                ],
            }
        ],
    }


def _sample_failure():
    return {
        "version": "failure",
        "classifications": [
            {
                "candidate": "candidate_a",
                "drawdown_scaling_ratio": 0.2,
                "failure_modes": [
                    "stock_alpha_sleeve_decay",
                    "defensive_sleeve_under_offset",
                    "regime_detection_lag_or_under_de_risking",
                    "drawdown_scaling_active_after_damage",
                ],
            }
        ],
    }


def _sample_decay():
    return {
        "version": "decay",
        "candidates": [
            {
                "candidate": "candidate_a",
                "dominant_negative_sleeves": [
                    {
                        "sleeve": "contrib_price_lowvol_reversal",
                        "recent_sum": -0.013,
                        "worst_recent_month": "2026-05",
                    },
                    {
                        "sleeve": "contrib_price_ic_diversified",
                        "recent_sum": -0.009,
                        "worst_recent_month": "2026-05",
                    },
                ],
            }
        ],
    }


def test_repair_plan_prioritizes_stock_decay_before_parameter_tuning() -> None:
    module = _load_module("plan_v29_recent90_repair")

    report = module.build_report(
        _sample_regime(),
        _sample_failure(),
        _sample_decay(),
        datetime(2026, 6, 29, tzinfo=timezone.utc),
    )

    candidate = report["candidates"][0]
    assert report["production_ready"] is False
    assert report["not_parameter_tuning"] is True
    assert report["decision"]["should_change_strategy_parameters_now"] is False
    assert candidate["repair_actions"][0]["action"] == "stock_sleeve_decay_root_cause"
    assert "do not flip factor signs" in candidate["repair_actions"][0]["do_not_do"]


def test_repair_plan_flags_regime_and_defensive_followups() -> None:
    module = _load_module("plan_v29_recent90_repair")

    report = module.build_report(
        _sample_regime(),
        _sample_failure(),
        _sample_decay(),
        datetime(2026, 6, 29, tzinfo=timezone.utc),
    )

    actions = {action["action"] for action in report["candidates"][0]["repair_actions"]}
    assert "regime_trigger_timing_audit" in actions
    assert "defensive_offset_quality_audit" in actions
    assert "drawdown_scaling_lag_audit" in actions
    assert "root-cause diagnostics" in report["decision"]["next_step_class"]


def test_repair_plan_writes_newline_terminated_outputs(tmp_path) -> None:
    module = _load_module("plan_v29_recent90_repair")
    report = module.build_report(
        _sample_regime(),
        _sample_failure(),
        _sample_decay(),
        datetime(2026, 6, 29, tzinfo=timezone.utc),
    )
    json_path = tmp_path / "plan.json"
    md_path = tmp_path / "plan.md"

    module._write_json(json_path, report)
    module._write_markdown(md_path, report)

    assert json_path.read_bytes().endswith(b"\n")
    assert md_path.read_bytes().endswith(b"\n")
    assert "should_change_strategy_parameters_now: false" in md_path.read_text(encoding="utf-8")
