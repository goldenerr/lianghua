import importlib.util
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def test_all_operational_scripts_are_syntax_valid_and_have_no_legacy_absolute_path() -> None:
    for script in SCRIPTS.glob("*.py"):
        source = script.read_text(encoding="utf-8")
        compile(source, str(script), "exec")
        assert "/home/hermes/.hermes/projects/lianghua" not in source


def test_script_paths_support_external_data_volumes(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("QUANT_PROJECT_DIR", str(tmp_path / "app"))
    monkeypatch.setenv("QUANT_DATA_DIR", str(tmp_path / "market-data"))
    spec = importlib.util.spec_from_file_location("script_paths_test", SCRIPTS / "_paths.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert tmp_path / "app" == module.PROJECT_DIR
    assert tmp_path / "market-data" == module.DATA_DIR
    assert tmp_path / "app" / "data" / "backtest_results" == module.RESULTS_DIR
