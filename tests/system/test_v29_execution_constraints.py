import importlib.util
import sys
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def _load_module():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "research_v29_portfolio_layer",
        SCRIPTS / "research_v29_portfolio_layer.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_execution_constraints_block_limit_up_buys_and_leave_cash() -> None:
    module = _load_module()

    constrained, diagnostics = module._apply_execution_constraints(
        old_positions={"000001": 0.5, "000002": 0.5},
        target_positions={"000002": 0.4, "000003": 0.6},
        is_tradable=pd.Series({"000001": True, "000002": True, "000003": True}),
        is_limit_up=pd.Series({"000001": False, "000002": False, "000003": True}),
        is_limit_down=pd.Series({"000001": False, "000002": False, "000003": False}),
    )

    assert constrained == {"000002": 0.4}
    assert diagnostics["blocked_buy_orders"] == 1.0
    assert diagnostics["blocked_buy_weight"] == 0.6
    assert diagnostics["cash_weight_after_constraints"] == 0.6


def test_execution_constraints_block_limit_down_sells_without_leverage() -> None:
    module = _load_module()

    constrained, diagnostics = module._apply_execution_constraints(
        old_positions={"000001": 0.5, "000002": 0.5},
        target_positions={"000002": 0.5, "000003": 0.5},
        is_tradable=pd.Series({"000001": True, "000002": True, "000003": True}),
        is_limit_up=pd.Series({"000001": False, "000002": False, "000003": False}),
        is_limit_down=pd.Series({"000001": True, "000002": False, "000003": False}),
    )

    assert constrained["000001"] == 0.5
    assert constrained["000002"] == 0.5
    assert constrained.get("000003", 0.0) == 0.0
    assert sum(constrained.values()) <= 1.0
    assert diagnostics["blocked_sell_orders"] == 1.0
    assert diagnostics["blocked_sell_weight"] == 0.5


def test_load_trading_status_constraints_pivots_v31_shape(tmp_path) -> None:
    module = _load_module()
    path = tmp_path / "status.parquet"
    frame = pd.DataFrame(
        [
            {
                "date": "2024-01-02",
                "code": "000001",
                "is_tradable": True,
                "is_limit_up": False,
                "is_limit_down": False,
                "is_st_known": False,
            },
            {
                "date": "2024-01-02",
                "code": "000002",
                "is_tradable": False,
                "is_limit_up": False,
                "is_limit_down": True,
                "is_st_known": False,
            },
        ]
    )
    frame.to_parquet(path, index=False)

    constraints, summary = module._load_trading_status_constraints(
        path,
        pd.DatetimeIndex([pd.Timestamp("2024-01-02")]),
        pd.Index(["000001", "000002", "000003"]),
    )

    assert constraints is not None
    assert summary["enabled"] is True
    assert summary["unique_symbols"] == 2
    assert bool(constraints["is_tradable"].loc[pd.Timestamp("2024-01-02"), "000001"])
    assert not bool(constraints["is_tradable"].loc[pd.Timestamp("2024-01-02"), "000003"])
