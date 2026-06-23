import importlib.util
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def _load_module():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "prepare_v40_paper_shadow_package",
        SCRIPTS / "prepare_v40_paper_shadow_package.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_package_remains_blocked_when_robustness_fails(tmp_path: Path) -> None:
    module = _load_module()
    research = {
        "results": [
            {
                "scenario": {
                    "capital": 200000,
                    "stock_alpha_cap": 0.07,
                    "crisis_cap": 0.20,
                    "stock_rebalance_freq": 40,
                },
                "full": {"annual_return": 0.06},
                "gates": {"relative_improvement_gate_passes": True},
            }
        ]
    }
    robustness = {
        "candidate": {"capital": 200000, "stock_cap": 0.07, "crisis_cap": 0.20},
        "robustness_gate_passes": False,
        "blockers": ["stress_sensitivity_pass_ratio_passes"],
        "gates": {
            "candidate_default_relative_passes": True,
            "candidate_cost_stress_relative_passes": True,
        },
        "candidate_metrics": {"default": {}, "cost_stress": {}},
    }
    research_path = tmp_path / "research.json"
    robustness_path = tmp_path / "robustness.json"
    research_path.write_text(json.dumps(research), encoding="utf-8")
    robustness_path.write_text(json.dumps(robustness), encoding="utf-8")

    package = module.build_package(research_path, robustness_path)

    assert package["shadow_candidate_exists"] is True
    assert package["paper_shadow_ready"] is False
    assert "stress_sensitivity_pass_ratio_passes" in package["blockers"]


def test_runbook_contains_no_secret_material(tmp_path: Path) -> None:
    module = _load_module()
    package = {
        "ts": "2026-06-23T00:00:00+00:00",
        "paper_shadow_ready": False,
        "candidate": {
            "capital": 200000,
            "stock_alpha_cap": 0.07,
            "crisis_cap": 0.20,
            "stock_rebalance_freq": 40,
        },
        "blockers": ["approved external WORM archive evidence"],
        "required_shadow_controls": ["read-only market data subscription"],
    }
    path = tmp_path / "runbook.md"

    module._write_runbook(path, package)
    content = path.read_text(encoding="utf-8")

    assert "token" not in content.lower()
    assert "api_key" not in content.lower()
