import importlib.util
import inspect
import sys
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def _load_module(file_name: str, module_name: str):
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / file_name)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _v37():
    return _load_module(
        "research_v37_stock_alpha_multi_asset_budget.py",
        "research_v37_stock_alpha_multi_asset_budget",
    )


def test_v33_and_v36_return_series_interfaces_are_opt_in() -> None:
    v33 = _load_module("research_v33_small_account_execution.py", "research_v33_for_v37_test")
    v36 = _load_module("research_v36_multi_asset_regime_budget.py", "research_v36_for_v37_test")

    assert inspect.signature(v33._run_scenario).parameters["include_return_series"].default is False
    assert inspect.signature(v36._run_scenario).parameters["include_return_series"].default is False


def test_risk_off_regime_blocks_stock_alpha_allocation() -> None:
    module = _v37()
    dates = pd.bdate_range("2024-01-02", periods=140)
    etf = pd.Series(0.0001, index=dates)
    stock = pd.Series(0.0010, index=dates)
    regimes = pd.Series("risk_off", index=dates)

    _combined, weights, diagnostics = module._combine_sleeves(
        etf_returns=etf,
        etf_risky_exposure=pd.Series(0.10, index=dates),
        stock_returns=stock,
        regimes=regimes,
        alpha_cap=0.20,
        overlay_rebalance_freq=20,
        alpha_lookback=40,
        overlay_cost_bps=5.0,
    )

    assert float(weights.max()) == 0.0
    assert diagnostics["active_alpha_days"] == 0


def test_future_shock_does_not_change_pre_shock_allocation() -> None:
    module = _v37()
    dates = pd.bdate_range("2024-01-02", periods=160)
    etf = pd.Series(0.0001, index=dates)
    stock_base = pd.Series(0.0010, index=dates)
    stock_with_future_shock = stock_base.copy()
    stock_with_future_shock.iloc[100] = -0.50
    regimes = pd.Series("risk_on", index=dates)

    _base_returns, base_weights, _base_diag = module._combine_sleeves(
        etf_returns=etf,
        etf_risky_exposure=pd.Series(0.10, index=dates),
        stock_returns=stock_base,
        regimes=regimes,
        alpha_cap=0.20,
        overlay_rebalance_freq=20,
        alpha_lookback=40,
        overlay_cost_bps=5.0,
    )
    _shock_returns, shock_weights, _shock_diag = module._combine_sleeves(
        etf_returns=etf,
        etf_risky_exposure=pd.Series(0.10, index=dates),
        stock_returns=stock_with_future_shock,
        regimes=regimes,
        alpha_cap=0.20,
        overlay_rebalance_freq=20,
        alpha_lookback=40,
        overlay_cost_bps=5.0,
    )

    pd.testing.assert_series_equal(base_weights.iloc[:101], shock_weights.iloc[:101])
    assert float(base_weights.max()) <= 0.20
    assert float(shock_weights.max()) <= 0.20


def test_etf_risk_headroom_caps_alpha_allocation() -> None:
    module = _v37()
    dates = pd.bdate_range("2024-01-02", periods=140)
    etf = pd.Series(0.0001, index=dates)
    stock = pd.Series(0.0010, index=dates)
    regimes = pd.Series("risk_on", index=dates)
    etf_exposure = pd.Series(0.90, index=dates)

    _combined, weights, diagnostics = module._combine_sleeves(
        etf_returns=etf,
        etf_risky_exposure=etf_exposure,
        stock_returns=stock,
        regimes=regimes,
        alpha_cap=0.20,
        overlay_rebalance_freq=20,
        alpha_lookback=40,
        overlay_cost_bps=5.0,
    )

    assert float(weights.max()) <= 0.04 + 1e-12
    assert diagnostics["max_estimated_total_risk_exposure"] <= 0.95 + 1e-12


def test_simultaneous_gate_requires_return_risk_oos_and_execution() -> None:
    module = _v37()
    baseline_full = {"annual_return": 0.06, "sharpe_ratio": 0.50, "max_drawdown": -0.12}
    baseline_wf = {"avg_oos_sharpe": 0.30, "folds": [{"oos": 0.10}]}
    improved = {
        "annual_return": 0.07,
        "sharpe_ratio": 0.60,
        "max_drawdown": -0.13,
        "win_rate": 0.45,
    }
    wf = {"avg_oos_sharpe": 0.40, "folds": [{"oos": 0.20}]}
    executable_stock = {"avg_holdings": 0.75, "orders": 20, "cash_blocked_buys": 0}

    passing = module._evaluate_gates(
        full=improved,
        wf=wf,
        baseline_full=baseline_full,
        baseline_wf=baseline_wf,
        stock_full=executable_stock,
        allocation_diagnostics={"max_estimated_total_risk_exposure": 0.94},
    )
    failed_execution = module._evaluate_gates(
        full=improved,
        wf=wf,
        baseline_full=baseline_full,
        baseline_wf=baseline_wf,
        stock_full={"avg_holdings": 0.0, "orders": 0, "cash_blocked_buys": 0},
        allocation_diagnostics={"max_estimated_total_risk_exposure": 0.94},
    )
    failed_capacity = module._evaluate_gates(
        full=improved,
        wf=wf,
        baseline_full=baseline_full,
        baseline_wf=baseline_wf,
        stock_full=executable_stock,
        allocation_diagnostics={"max_estimated_total_risk_exposure": 0.951},
    )

    assert passing["simultaneous_improvement_gate_passes"] is True
    assert failed_execution["simultaneous_improvement_gate_passes"] is False
    assert failed_capacity["simultaneous_improvement_gate_passes"] is False


def test_risk_scale_rejects_insufficient_history() -> None:
    module = _v37()
    scale, realized_vol, daily_var = module._historical_risk_scale(
        pd.Series([0.001] * 20),
        target_vol=0.13,
        daily_var_limit=0.018,
    )

    assert scale == 0.0
    assert realized_vol == 0.0
    assert daily_var == 0.0
