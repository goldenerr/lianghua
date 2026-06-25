from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_run_production_tests():
    script = Path(__file__).resolve().parents[2] / "scripts" / "run_production_tests.py"
    sys.path.insert(0, str(script.parent))
    spec = importlib.util.spec_from_file_location("run_production_tests", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_legacy_v5_diagnostic_does_not_fail_suite_on_alpha_gates(tmp_path, monkeypatch):
    """Legacy V5 is a risk regression diagnostic; weak alpha gates belong to V29."""
    module = _load_run_production_tests()
    result_dir = tmp_path / "data" / "backtest_results"
    result_dir.mkdir(parents=True)
    monkeypatch.setattr(module, "PROJECT", tmp_path)

    (result_dir / "wf_backtest_v5.2_report.json").write_text(
        json.dumps({"full": {"sharpe_ratio": 1.39, "max_drawdown": -0.28}}),
        encoding="utf-8",
    )
    (result_dir / "wf_backtest_v5.9_report.json").write_text(
        json.dumps(
            {
                "full": {"sharpe_ratio": -0.12, "max_drawdown": -0.19},
                "gates": {"S": False, "M": False, "D": False, "W": True},
            }
        ),
        encoding="utf-8",
    )

    assert module.check_legacy_v5_wf_diagnostic() is True
