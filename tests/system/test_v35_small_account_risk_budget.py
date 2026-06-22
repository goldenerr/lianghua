import importlib.util
import sys
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def _load_module():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "research_v35_small_account_risk_budget",
        SCRIPTS / "research_v35_small_account_risk_budget.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_risk_symbols_excludes_cash_proxy_and_requires_signal() -> None:
    module = _load_module()
    scenario = module.RiskBudgetScenario(
        capital=50_000,
        max_assets=2,
        rebalance_freq=20,
        rank_mode="momentum",
        risk_profile="ultra_var",
        target_vol=0.04,
        daily_var_limit=0.008,
    )
    score = pd.Series({"510300": 0.05, "510500": 0.03, "511880": 1.0})

    assert module._risk_symbols(score, scenario) == ["510300"]


def test_historical_budget_is_capped_by_var_and_volatility() -> None:
    module = _load_module()
    dates = pd.bdate_range("2024-01-02", periods=160)
    returns = [0.006, -0.005, 0.007, -0.006]
    values_300 = [100.0]
    values_500 = [100.0]
    for i in range(1, 160):
        values_300.append(values_300[-1] * (1.0 + returns[i % len(returns)]))
        values_500.append(values_500[-1] * (1.0 + returns[i % len(returns)] * 0.8))
    close = pd.DataFrame({"510300": values_300, "510500": values_500}, index=dates)
    scenario = module.RiskBudgetScenario(
        capital=50_000,
        max_assets=2,
        rebalance_freq=20,
        rank_mode="momentum",
        risk_profile="balanced_var",
        target_vol=0.04,
        daily_var_limit=0.004,
    )

    budget, diagnostics = module._historical_budget(
        close,
        signal_idx=159,
        selected=["510300", "510500"],
        scenario=scenario,
        nav=50_000.0,
        peak_nav=50_000.0,
    )

    assert 0.0 < budget < module.RISK_PROFILES["balanced_var"]["max_risky_budget"]
    assert diagnostics["realized_vol"] > scenario.target_vol
    assert diagnostics["daily_var95"] > scenario.daily_var_limit


def test_drawdown_stop_sets_budget_to_zero() -> None:
    module = _load_module()
    dates = pd.bdate_range("2024-01-02", periods=160)
    close = pd.DataFrame({"510300": [100.0 + i * 0.1 for i in range(160)]}, index=dates)
    scenario = module.RiskBudgetScenario(
        capital=50_000,
        max_assets=1,
        rebalance_freq=20,
        rank_mode="momentum",
        risk_profile="conservative_var",
        target_vol=0.08,
        daily_var_limit=0.02,
    )

    budget, diagnostics = module._historical_budget(
        close,
        signal_idx=159,
        selected=["510300"],
        scenario=scenario,
        nav=44_000.0,
        peak_nav=50_000.0,
    )

    assert budget == 0.0
    assert diagnostics["drawdown_scale"] == 0.0


def test_target_weights_leave_unallocated_budget_as_cash() -> None:
    module = _load_module()
    dates = pd.bdate_range("2024-01-02", periods=160)
    close = pd.DataFrame(
        {
            "510300": [100.0 + i * 0.2 for i in range(160)],
            "511880": [100.0 + i * 0.01 for i in range(160)],
        },
        index=dates,
    )
    scenario = module.RiskBudgetScenario(
        capital=50_000,
        max_assets=1,
        rebalance_freq=20,
        rank_mode="momentum",
        risk_profile="balanced_var",
        target_vol=0.04,
        daily_var_limit=0.02,
    )
    score = pd.Series({"510300": 0.2, "511880": 1.0})

    weights, diagnostics = module._risk_budget_target_weights(
        close,
        signal_idx=159,
        score=score,
        scenario=scenario,
        nav=50_000.0,
        peak_nav=50_000.0,
    )

    assert "511880" not in weights
    assert 0.0 < sum(weights.values()) < 1.0
    assert diagnostics["final_budget"] == sum(weights.values())
