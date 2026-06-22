import importlib.util
import sys
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def _load_module():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "research_v33_small_account_execution",
        SCRIPTS / "research_v33_small_account_execution.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_round_down_to_a_share_lot() -> None:
    module = _load_module()

    assert module._round_down_to_lot(99, 100) == 0
    assert module._round_down_to_lot(100, 100) == 100
    assert module._round_down_to_lot(299, 100) == 200
    assert module._round_down_to_lot(-1, 100) == 0


def test_minimum_commission_and_sell_stamp_are_applied() -> None:
    module = _load_module()
    cost = module.CostConfig(
        commission_rate=0.00025,
        min_commission=5.0,
        stamp_duty_rate=0.0005,
        slippage_bps=5.0,
    )

    assert module._order_fee(1000.0, "buy", cost) == 5.5
    assert module._order_fee(1000.0, "sell", cost) == 6.0
    assert module._order_fee(100000.0, "buy", cost) == 75.0


def test_affordable_buy_shares_steps_down_by_lot() -> None:
    module = _load_module()
    cost = module.CostConfig(min_commission=5.0, slippage_bps=5.0)

    shares = module._affordable_buy_shares(
        cash=1005.0,
        price=10.0,
        desired_shares=200,
        lot_size=100,
        cost=cost,
    )

    assert shares == 0

    shares = module._affordable_buy_shares(
        cash=1006.0,
        price=10.0,
        desired_shares=200,
        lot_size=100,
        cost=cost,
    )

    assert shares == 100


def test_rebalance_blocks_limit_up_buy_and_limit_down_sell() -> None:
    module = _load_module()
    dates = pd.DatetimeIndex([pd.Timestamp("2024-01-02")])
    constraints = {
        "is_tradable": pd.DataFrame(True, index=dates, columns=["000001", "000002", "000003"]),
        "is_limit_up": pd.DataFrame(False, index=dates, columns=["000001", "000002", "000003"]),
        "is_limit_down": pd.DataFrame(False, index=dates, columns=["000001", "000002", "000003"]),
    }
    constraints["is_limit_down"].loc[dates[0], "000001"] = True
    constraints["is_limit_up"].loc[dates[0], "000003"] = True
    scenario = module.ScenarioConfig(capital=50_000, max_positions=2, rank_mode="blend")

    positions, cash, diagnostics = module._execute_rebalance(
        positions={"000001": 100},
        cash=10_000.0,
        target_symbols=["000003"],
        target_gross=0.5,
        nav=20_000.0,
        trade_prices=pd.Series({"000001": 10.0, "000003": 10.0}),
        trade_date=dates[0],
        trading_constraints=constraints,
        scenario=scenario,
        cost=module.CostConfig(),
    )

    assert positions == {"000001": 100}
    assert cash == 10_000.0
    assert diagnostics["blocked_sells"] == 1.0
    assert diagnostics["blocked_buys"] == 1.0
