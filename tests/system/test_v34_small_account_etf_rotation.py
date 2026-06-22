import importlib.util
import sys
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def _load_module():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "research_v34_small_account_etf_rotation",
        SCRIPTS / "research_v34_small_account_etf_rotation.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_load_etf_panel_reads_benchmark_files(tmp_path) -> None:
    module = _load_module()
    dates = pd.bdate_range("2024-01-02", periods=3)
    frame = pd.DataFrame({"close": [1.0, 1.1, 1.2]}, index=dates)
    frame.index.name = "date"
    frame.to_parquet(tmp_path / "etf_510300.parquet")

    panel, metadata = module._load_etf_panel(tmp_path, ["510300", "511880"])

    assert list(panel.columns) == ["510300"]
    assert metadata["510300"]["rows"] == 3
    assert metadata["511880"]["exists"] is False


def test_ultra_guard_negative_signal_targets_cash_proxy() -> None:
    module = _load_module()
    scenario = module.EtfScenario(
        capital=50_000,
        max_assets=1,
        rebalance_freq=20,
        rank_mode="momentum",
        risk_profile="ultra_guard",
    )
    score = pd.Series({"510300": -0.01, "511880": 0.001})

    weights = module._target_weights(score, scenario)

    assert weights == {"511880": 1.0}


def test_capital_guard_shifts_risk_to_cash_after_drawdown() -> None:
    module = _load_module()
    scenario = module.EtfScenario(
        capital=50_000,
        max_assets=1,
        rebalance_freq=20,
        rank_mode="momentum",
        risk_profile="ultra_guard",
    )
    score = pd.Series({"510300": 0.08, "511880": 0.001})

    guarded = module._apply_capital_guard(
        {"510300": 0.35, "511880": 0.65},
        score=score,
        nav=45_000.0,
        peak_nav=50_000.0,
        scenario=scenario,
    )

    assert guarded == {"511880": 1.0}
    assert abs(sum(guarded.values()) - 1.0) < 1e-12


def test_rebalance_respects_100_share_lot_and_min_fee() -> None:
    module = _load_module()
    scenario = module.EtfScenario(
        capital=50_000,
        max_assets=1,
        rebalance_freq=20,
        rank_mode="momentum",
        risk_profile="balanced",
    )

    positions, cash, diagnostics = module._execute_rebalance(
        positions={},
        cash=1_006.0,
        target_weights={"510300": 1.0},
        nav=1_006.0,
        prices=pd.Series({"510300": 10.0}),
        scenario=scenario,
        cost=module.CostConfig(min_commission=5.0, stamp_duty_rate=0.0, slippage_bps=5.0),
    )

    assert positions == {"510300": 100}
    assert 0.0 <= cash <= 1.0
    assert diagnostics["buy_orders"] == 1.0
