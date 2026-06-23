import importlib.util
import sys
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def _load_module():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "research_v39_long_history_crisis_basket",
        SCRIPTS / "research_v39_long_history_crisis_basket.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _trend_panel() -> pd.DataFrame:
    dates = pd.bdate_range("2023-01-02", periods=180)
    return pd.DataFrame(
        {
            "518880": [100.0 + i * 0.20 for i in range(180)],
            "511010": [100.0 + i * 0.10 for i in range(180)],
            "511260": [100.0 - i * 0.02 for i in range(180)],
        },
        index=dates,
    )


def test_crisis_signal_does_not_use_future_prices() -> None:
    module = _load_module()
    close = _trend_panel()
    shocked = close.copy()
    shocked.iloc[160, 0] = 1.0

    base, _ = module._crisis_target_weights(
        close,
        signal_idx=159,
        regime="risk_off",
        crisis_cap=0.20,
        max_assets=2,
        allocation_mode="equal_pair",
    )
    changed, _ = module._crisis_target_weights(
        shocked,
        signal_idx=159,
        regime="risk_off",
        crisis_cap=0.20,
        max_assets=2,
        allocation_mode="equal_pair",
    )

    assert changed == base


def test_pair_selection_contains_gold_and_one_bond() -> None:
    module = _load_module()
    weights, diagnostics = module._crisis_target_weights(
        _trend_panel(),
        signal_idx=159,
        regime="risk_off",
        crisis_cap=0.20,
        max_assets=2,
        allocation_mode="equal_pair",
    )

    assert "518880" in weights
    assert set(weights).intersection(module.BOND_SYMBOLS)
    assert abs(sum(weights.values()) - 0.20) < 1e-12
    assert len(diagnostics["selected"]) == 2


def test_risk_on_regime_disables_crisis_budget() -> None:
    module = _load_module()
    weights, diagnostics = module._crisis_target_weights(
        _trend_panel(),
        signal_idx=159,
        regime="risk_on",
        crisis_cap=0.20,
        max_assets=2,
        allocation_mode="inverse_vol_pair",
    )

    assert weights == {}
    assert diagnostics["target_budget"] == 0.0


def test_target_merge_respects_shared_capacity() -> None:
    module = _load_module()
    merged, diagnostics = module._merge_targets(
        {"510300": 0.90},
        {"518880": 0.10, "511010": 0.10},
    )

    assert abs(sum(merged.values()) - module.TARGET_RISK_CAPACITY) < 1e-12
    assert diagnostics["crisis_target"] == 0.04


def test_crisis_assets_cannot_shorten_core_calendar() -> None:
    module = _load_module()
    core_dates = pd.bdate_range("2020-01-02", periods=10)
    crisis_dates = core_dates[5:]
    core = pd.DataFrame({"510300": range(10)}, index=core_dates)
    crisis = pd.DataFrame({"511010": range(5)}, index=crisis_dates)

    aligned_core, aligned_crisis = module._align_to_core_calendar(core, crisis)

    assert aligned_core.index.equals(core_dates)
    assert aligned_crisis.index.equals(core_dates)
    assert aligned_crisis.iloc[:5].isna().all().all()


def test_asset_evidence_marks_missing_request_provenance(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "asset.parquet"
    pd.DataFrame(
        {"close": [1.0, 1.1]},
        index=pd.DatetimeIndex(["2024-01-02", "2024-01-03"]),
    ).to_parquet(path)

    evidence = module._asset_data_evidence({"path": str(path)})

    assert evidence["request_range_provenance"] is False
    assert len(evidence["sha256"]) == 64
