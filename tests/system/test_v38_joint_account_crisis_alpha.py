import importlib.util
import sys
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def _load_module():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "research_v38_joint_account_crisis_alpha",
        SCRIPTS / "research_v38_joint_account_crisis_alpha.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _constraints(date: pd.Timestamp, symbol: str, *, limit_down: bool = False):
    index = pd.DatetimeIndex([date])
    return {
        "is_tradable": pd.DataFrame({symbol: [True]}, index=index),
        "is_limit_up": pd.DataFrame({symbol: [False]}, index=index),
        "is_limit_down": pd.DataFrame({symbol: [limit_down]}, index=index),
    }


def test_gold_crisis_signal_uses_only_lagged_history() -> None:
    module = _load_module()
    dates = pd.bdate_range("2023-01-02", periods=150)
    close = pd.DataFrame({"518880": [100.0 + i * 0.2 for i in range(150)]}, index=dates)
    shocked = close.copy()
    shocked.iloc[140, 0] = 1.0

    base_weight, _base_diag = module._crisis_target_weight(
        close,
        signal_idx=139,
        regime="risk_off",
        crisis_cap=0.15,
    )
    shocked_weight, _shock_diag = module._crisis_target_weight(
        shocked,
        signal_idx=139,
        regime="risk_off",
        crisis_cap=0.15,
    )

    assert base_weight == 0.15
    assert shocked_weight == base_weight


def test_target_merge_prioritizes_crisis_without_exceeding_capacity() -> None:
    module = _load_module()
    weights, diagnostics = module._merge_target_weights(
        etf_weights={"510300": 0.80},
        stock_symbols=["000001", "000002"],
        stock_budget=0.20,
        crisis_weight=0.10,
    )

    assert diagnostics["total_target"] == 0.94
    assert diagnostics["crisis_target"] == 0.10
    assert diagnostics["stock_target"] == 0.04
    assert abs(sum(weights.values()) - 0.94) < 1e-12


def test_target_merge_supports_multi_asset_crisis_basket() -> None:
    module = _load_module()
    weights, diagnostics = module._merge_target_weights(
        etf_weights={"510300": 0.70},
        stock_symbols=[],
        stock_budget=0.0,
        crisis_weights={"518880": 0.10, "511010": 0.10},
    )

    assert weights["518880"] == 0.10
    assert weights["511010"] == 0.10
    assert diagnostics["crisis_target"] == 0.20


def test_joint_execution_sells_before_buying_and_preserves_nonnegative_cash() -> None:
    module = _load_module()
    date = pd.Timestamp("2025-01-02")
    stock_symbol = "000001"
    positions, cash, diagnostics = module._execute_joint_rebalance(
        positions={"510300": 100},
        cash=0.0,
        target_weights={stock_symbol: 0.80},
        nav=10_000.0,
        prices=pd.Series({"510300": 100.0, stock_symbol: 10.0}),
        trade_date=date,
        stock_symbols={stock_symbol},
        trading_constraints=_constraints(date, stock_symbol),
        stock_cost=module.CostConfig(slippage_bps=0.0),
        etf_cost=module.CostConfig(stamp_duty_rate=0.0, slippage_bps=0.0),
        lot_size=100,
    )

    assert "510300" not in positions
    assert positions[stock_symbol] > 0
    assert cash >= 0.0
    assert diagnostics["sell_orders"] == 1
    assert diagnostics["buy_orders"] == 1


def test_limit_down_stock_sell_is_blocked() -> None:
    module = _load_module()
    date = pd.Timestamp("2025-01-02")
    stock_symbol = "000001"
    positions, cash, diagnostics = module._execute_joint_rebalance(
        positions={stock_symbol: 100},
        cash=100.0,
        target_weights={},
        nav=1_100.0,
        prices=pd.Series({stock_symbol: 10.0}),
        trade_date=date,
        stock_symbols={stock_symbol},
        trading_constraints=_constraints(date, stock_symbol, limit_down=True),
        stock_cost=module.CostConfig(slippage_bps=0.0),
        etf_cost=module.CostConfig(stamp_duty_rate=0.0, slippage_bps=0.0),
        lot_size=100,
    )

    assert positions == {stock_symbol: 100}
    assert cash == 100.0
    assert diagnostics["blocked_sells"] == 1
    assert diagnostics["sell_orders"] == 0


def test_stock_budget_blocks_negative_trailing_alpha() -> None:
    module = _load_module()
    dates = pd.bdate_range("2024-01-02", periods=80)
    returns = pd.Series(-0.001, index=dates)

    budget, diagnostics = module._stock_target_budget(
        returns,
        signal_date=dates[-1],
        regime="risk_on",
        stock_alpha_cap=0.20,
        nav=100_000.0,
        peak_nav=100_000.0,
        lookback=60,
    )

    assert budget == 0.0
    assert diagnostics["trailing_stock_return"] < 0.0


def test_baseline_parity_rejects_single_day_return_drift() -> None:
    module = _load_module()
    dates = pd.bdate_range("2025-01-02", periods=3)
    reference = pd.Series([0.01, -0.02, 0.03], index=dates)
    identical = module._compare_baseline_returns(reference.copy(), reference)
    drifted = reference.copy()
    drifted.iloc[1] += 1e-6

    assert identical["passes"] is True
    assert module._compare_baseline_returns(drifted, reference)["passes"] is False


def test_stock_ready_index_can_align_to_etf_rebalances() -> None:
    module = _load_module()

    aligned = module._stock_ready_index(
        start_idx=127,
        raw_ready_idx=253,
        etf_rebalance_freq=20,
        align_to_etf=True,
    )

    assert aligned == 267
    assert (aligned - 127) % 20 == 0
