import importlib.util
import sys
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def _load_module():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "research_v36_multi_asset_regime_budget",
        SCRIPTS / "research_v36_multi_asset_regime_budget.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_etf(path: Path, closes: list[float]) -> None:
    dates = pd.bdate_range("2020-01-02", periods=len(closes))
    frame = pd.DataFrame({"close": closes}, index=dates)
    frame.index.name = "date"
    frame.to_parquet(path)


def test_core_long_universe_excludes_short_history_star50(tmp_path) -> None:
    module = _load_module()
    benchmark_dir = tmp_path / "benchmarks"
    hedge_dir = tmp_path / "hedge"
    benchmark_dir.mkdir()
    hedge_dir.mkdir()
    _write_etf(benchmark_dir / "etf_510300.parquet", [1.0 + i * 0.001 for i in range(240)])
    _write_etf(benchmark_dir / "etf_588000.parquet", [1.0 + i * 0.001 for i in range(60)])

    _panel, metadata, roles = module._load_multi_asset_panel(
        benchmark_dir,
        hedge_dir,
        "core_long",
        long_history_min_days=120,
    )

    assert "510300" in roles
    assert "588000" not in roles
    assert metadata["510300"]["long_history_ready"] is True
    assert metadata["510300"]["exists"] is True


def test_expanded_recent_marks_short_history_assets(tmp_path) -> None:
    module = _load_module()
    benchmark_dir = tmp_path / "benchmarks"
    hedge_dir = tmp_path / "hedge"
    benchmark_dir.mkdir()
    hedge_dir.mkdir()
    _write_etf(benchmark_dir / "etf_510300.parquet", [1.0 + i * 0.001 for i in range(240)])
    _write_etf(benchmark_dir / "etf_588000.parquet", [1.0 + i * 0.001 for i in range(60)])
    _write_etf(hedge_dir / "etf_159920.parquet", [1.0 + i * 0.001 for i in range(60)])

    _panel, metadata, _roles = module._load_multi_asset_panel(
        benchmark_dir,
        hedge_dir,
        "expanded_recent",
        long_history_min_days=120,
    )
    coverage = module._coverage_summary(metadata, long_history_min_days=120)

    assert coverage["short_history_only"] is True
    assert "588000" in coverage["short_history_symbols"]
    assert "159920" in coverage["short_history_symbols"]


def test_market_regime_distinguishes_risk_on_and_risk_off() -> None:
    module = _load_module()
    dates = pd.bdate_range("2024-01-02", periods=160)
    rising = pd.DataFrame(
        {
            "510300": [100.0 + i * 0.3 for i in range(160)],
            "510500": [100.0 + i * 0.4 for i in range(160)],
        },
        index=dates,
    )
    falling = pd.DataFrame(
        {
            "510300": [160.0 - i * 0.3 for i in range(160)],
            "510500": [160.0 - i * 0.4 for i in range(160)],
        },
        index=dates,
    )
    roles = {"510300": "cn_large_equity", "510500": "cn_mid_equity"}

    risk_on, risk_on_diag = module._market_regime(rising, 159, roles)
    risk_off, risk_off_diag = module._market_regime(falling, 159, roles)

    assert risk_on in {"risk_on", "neutral"}
    assert risk_on_diag["equity_breadth_60d"] == 1.0
    assert risk_off == "risk_off"
    assert risk_off_diag["equity_breadth_60d"] == 0.0


def test_target_weights_exclude_cash_proxy_and_leave_cash_unallocated() -> None:
    module = _load_module()
    dates = pd.bdate_range("2024-01-02", periods=160)
    close = pd.DataFrame(
        {
            "510300": [100.0 + i * 0.2 for i in range(160)],
            "518880": [100.0 + i * 0.1 for i in range(160)],
            "511880": [100.0 + i * 0.01 for i in range(160)],
        },
        index=dates,
    )
    roles = {
        "510300": "cn_large_equity",
        "518880": "gold_defensive",
        "511880": "cash_proxy",
    }
    scenario = module.RegimeBudgetScenario(
        capital=50_000,
        universe_mode="core_long",
        max_assets=1,
        rebalance_freq=20,
        rank_mode="trend",
        risk_profile="aggressive",
    )
    score = pd.Series({"510300": 0.20, "518880": 0.10, "511880": 1.00})

    weights, diagnostics = module._target_weights(
        close,
        signal_idx=159,
        score=score,
        scenario=scenario,
        nav=50_000.0,
        peak_nav=50_000.0,
        roles=roles,
    )

    assert "511880" not in weights
    assert 0.0 < sum(weights.values()) < 1.0
    assert diagnostics["final_budget"] == sum(weights.values())
