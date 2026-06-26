import importlib.util
import sys
from datetime import datetime, timezone
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


def _sample_daily() -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-01", periods=120)
    rows = []
    for i, day in enumerate(dates):
        recent = i >= 30
        lowvol = -0.001 if recent else 0.0005
        diversified = -0.0006 if recent else 0.0003
        crisis = 0.00005 if recent else 0.0
        rows.append(
            {
                "candidate": "candidate_a",
                "date": day.date().isoformat(),
                "return": lowvol + diversified + crisis,
                "gross_stock_exposure": 0.3,
                "crisis_weight": 0.7,
                "carry_weight": 0.0,
                "risk_off": False,
                "mdd_state": "normal",
                "cost": 0.0,
                "crisis_contrib": crisis,
                "carry_contrib": 0.0,
                "contrib_price_lowvol_reversal": lowvol,
                "contrib_price_ic_diversified": diversified,
            }
        )
    return pd.DataFrame(rows)


def test_signal_decay_report_detects_stock_sleeve_drag() -> None:
    module = _load_module("diagnose_v29_recent90_signal_decay")

    report = module.build_report(
        _sample_daily(),
        recent_days=90,
        generated_at=datetime(2026, 6, 26, tzinfo=timezone.utc),
    )

    candidate = report["candidates"][0]
    assert report["production_ready"] is False
    assert report["not_parameter_tuning"] is True
    assert "stock_alpha_sleeves_are_primary_recent_drag" in candidate["diagnosis"]
    assert candidate["dominant_negative_sleeves"][0]["sleeve"].startswith("contrib_price_")


def test_signal_decay_flags_low_hit_rate_and_month_concentration() -> None:
    module = _load_module("diagnose_v29_recent90_signal_decay")
    frame = _sample_daily()

    result = module.analyze_candidate(frame, recent_days=90)

    sleeve = next(
        item for item in result["sleeves"] if item["sleeve"] == "contrib_price_lowvol_reversal"
    )
    assert "recent_negative" in sleeve["flags"]
    assert "low_recent_hit_rate" in sleeve["flags"]
    assert sleeve["recent_sum"] < 0


def test_signal_decay_requires_daily_columns() -> None:
    module = _load_module("diagnose_v29_recent90_signal_decay")

    try:
        module.build_report(
            pd.DataFrame({"candidate": ["a"]}),
            recent_days=90,
            generated_at=datetime.now(timezone.utc),
        )
    except RuntimeError as exc:
        assert "missing columns" in str(exc)
    else:
        raise AssertionError("expected missing-column RuntimeError")
