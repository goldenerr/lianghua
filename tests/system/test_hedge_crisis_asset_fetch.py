import importlib.util
import sys
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def _load_module():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "fetch_hedge_crisis_assets",
        SCRIPTS / "fetch_hedge_crisis_assets.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_frame(path: Path, *, with_request_range: bool, request_start: str = "20000101"):
    frame = pd.DataFrame(
        {"close": [1.0, 1.1]},
        index=pd.DatetimeIndex(["2026-05-28", "2026-05-29"], name="date"),
    )
    if with_request_range:
        frame["requested_start"] = request_start
        frame["requested_end"] = "20260529"
    frame.to_parquet(path)


def test_existing_file_without_request_range_is_not_current(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "legacy.parquet"
    _write_frame(path, with_request_range=False)

    assert module._is_current(path) is False


def test_full_requested_range_can_be_reused(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "complete.parquet"
    _write_frame(path, with_request_range=True)

    assert module._is_current(path) is True


def test_later_requested_start_cannot_satisfy_long_history(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "short.parquet"
    _write_frame(path, with_request_range=True, request_start="20240101")

    assert module._is_current(path) is False
