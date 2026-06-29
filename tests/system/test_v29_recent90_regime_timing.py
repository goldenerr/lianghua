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


def test_regime_trigger_state_applies_documented_thresholds() -> None:
    module = _load_module("diagnose_v29_recent90_regime_timing")

    assert module.classify_regime_state(mom_20=-0.031, mom_60=0.0, vol_20=0.20)["risk_off"] is True
    assert (
        module.classify_regime_state(mom_20=0.01, mom_60=-0.081, vol_20=0.20)["reason"]
        == "mom_60_breach"
    )
    assert (
        module.classify_regime_state(mom_20=0.01, mom_60=0.0, vol_20=0.341)["reason"]
        == "vol_20_breach"
    )
    assert module.classify_regime_state(mom_20=0.04, mom_60=0.03, vol_20=0.20)["regime"] == "bull"
    assert module.classify_regime_state(mom_20=0.0, mom_60=0.0, vol_20=0.20)["regime"] == "neutral"


def test_summarize_timing_window_flags_zero_risk_off_loss_month() -> None:
    module = _load_module("diagnose_v29_recent90_regime_timing")
    rows = pd.DataFrame(
        [
            {
                "date": "2026-05-06",
                "return": -0.01,
                "risk_off": False,
                "mom_20": 0.02,
                "mom_60": 0.03,
                "vol_20": 0.18,
            },
            {
                "date": "2026-05-07",
                "return": -0.02,
                "risk_off": False,
                "mom_20": 0.01,
                "mom_60": 0.02,
                "vol_20": 0.19,
            },
            {
                "date": "2026-05-08",
                "return": 0.005,
                "risk_off": False,
                "mom_20": 0.00,
                "mom_60": 0.01,
                "vol_20": 0.20,
            },
        ]
    )

    summary = module.summarize_timing_window("candidate_a", rows)

    assert summary["candidate"] == "candidate_a"
    assert summary["production_ready"] is False
    assert summary["risk_off_ratio"] == 0.0
    assert summary["compound_return"] < 0
    assert summary["diagnosis"] == "risk_off_missed_loss_window"
    assert summary["next_required_diagnostics"][0].startswith("Audit")


def test_join_regime_inputs_preserves_portfolio_dates() -> None:
    module = _load_module("diagnose_v29_recent90_regime_timing")
    portfolio_rows = pd.DataFrame(
        [
            {"date": "2026-05-06", "return": -0.01, "risk_off": False},
            {"date": "2026-05-07", "return": 0.02, "risk_off": True},
        ]
    )
    regime_rows = pd.DataFrame(
        [
            {
                "date": "2026-05-06",
                "mom_20": -0.01,
                "mom_60": 0.02,
                "vol_20": 0.2,
                "reason": "none",
            },
            {
                "date": "2026-05-07",
                "mom_20": -0.04,
                "mom_60": 0.01,
                "vol_20": 0.2,
                "reason": "mom_20_breach",
            },
        ]
    )

    joined = module.join_regime_inputs(portfolio_rows, regime_rows)

    assert list(joined["date"].astype(str)) == ["2026-05-06", "2026-05-07"]
    assert joined.loc[1, "reason"] == "mom_20_breach"
