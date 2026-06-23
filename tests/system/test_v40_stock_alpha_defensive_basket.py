import importlib.util
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def _load_module():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "research_v40_stock_alpha_defensive_basket",
        SCRIPTS / "research_v40_stock_alpha_defensive_basket.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_baseline_key_binds_capital_and_crisis_cap() -> None:
    module = _load_module()

    assert module._baseline_key(100_000.0, 0.15) == "100000:0.1500"
    assert module._baseline_key(100_000.0, 0.20) != module._baseline_key(100_000.0, 0.15)


def test_stock_execution_gate_rejects_zero_holdings() -> None:
    module = _load_module()
    row = {
        "full": {"avg_stock_holdings": 0.0, "max_stock_holdings": 0},
        "diagnostics": {"fees_by_group": {"stock_alpha": 0.0}},
    }

    assert module._stock_execution_gate(row) is False


def test_stock_execution_gate_requires_real_stock_fees() -> None:
    module = _load_module()
    row = {
        "full": {"avg_stock_holdings": 0.1, "max_stock_holdings": 2},
        "diagnostics": {"fees_by_group": {"stock_alpha": 10.0}},
    }

    assert module._stock_execution_gate(row) is True
