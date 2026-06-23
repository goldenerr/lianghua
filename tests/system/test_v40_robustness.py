import importlib.util
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def _load_module():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "validate_v40_robustness",
        SCRIPTS / "validate_v40_robustness.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_pass_ratio_counts_strict_relative_gate() -> None:
    module = _load_module()
    report = {
        "results": [
            {"gates": {"relative_improvement_gate_passes": True}},
            {"gates": {"relative_improvement_gate_passes": False}},
        ]
    }

    assert module._pass_ratio(report) == 0.5


def test_cross_capital_gate_requires_matching_candidate() -> None:
    module = _load_module()
    report = {
        "results": [
            {
                "scenario": {"capital": 50_000, "stock_alpha_cap": 0.07, "crisis_cap": 0.20},
                "gates": {"relative_improvement_gate_passes": True},
            },
            {
                "scenario": {"capital": 100_000, "stock_alpha_cap": 0.05, "crisis_cap": 0.20},
                "gates": {"relative_improvement_gate_passes": True},
            },
        ]
    }

    count, capitals = module._cross_capital_passes(report, stock_cap=0.07, crisis_cap=0.20)

    assert count == 1
    assert capitals == [50_000]
