import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def _load_module(name: str):
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_price_file(path: Path, dates: pd.DatetimeIndex, *, zero_volume_index: int | None = None) -> None:
    frame = pd.DataFrame(
        {
            "open": [10.0, 10.0, 11.0, 12.0][: len(dates)],
            "high": [10.1, 10.1, 11.1, 12.1][: len(dates)],
            "low": [9.9, 9.9, 10.9, 11.9][: len(dates)],
            "close": [10.0, 10.0, 11.0, 12.1][: len(dates)],
            "volume": [1000.0] * len(dates),
            "amount": [10000.0] * len(dates),
        },
        index=dates,
    )
    frame.index.name = "date"
    if zero_volume_index is not None:
        frame.iloc[zero_volume_index, frame.columns.get_loc("volume")] = 0.0
    frame.to_parquet(path)


def _build_sample_outputs(tmp_path: Path):
    module = _load_module("build_v31_free_data_pit_approximation")
    price_dir = tmp_path / "parquet"
    output_dir = tmp_path / "security_master" / "free_pit_approx"
    corporate_action_path = tmp_path / "corporate_actions" / "free_reconciliation_v31.parquet"
    price_dir.mkdir(parents=True)
    universe = tmp_path / "universe.json"
    universe.write_text('["000001", "300001"]', encoding="utf-8")
    dates = pd.bdate_range("2024-01-02", periods=4)
    _write_price_file(price_dir / "000001.parquet", dates, zero_volume_index=1)
    _write_price_file(price_dir / "300001.parquet", dates)

    codes = module._read_codes(universe)
    start = pd.Timestamp("2024-01-02")
    end = pd.Timestamp("2024-01-05")
    metadata, all_dates, failures = module._first_pass(
        codes=codes,
        price_dir=price_dir,
        start=start,
        end=end,
    )
    assert failures == []
    list(
        module._build_outputs(
            codes=codes,
            metadata=metadata,
            all_dates=all_dates,
            price_dir=price_dir,
            output_dir=output_dir,
            corporate_action_output=corporate_action_path,
            start=start,
            end=end,
            batch_size=1,
            generated_at=datetime(2026, 6, 18, tzinfo=timezone.utc),
        )
    )
    return output_dir, corporate_action_path


def test_v31_builds_free_pit_and_status_approximations(tmp_path) -> None:
    output_dir, corporate_action_path = _build_sample_outputs(tmp_path)
    pit = pd.read_parquet(output_dir / "pit_universe_v31.parquet")
    status = pd.read_parquet(output_dir / "trading_status_v31.parquet")
    actions = pd.read_parquet(corporate_action_path)

    assert set(pit["code"]) == {"000001", "300001"}
    assert pit["research_pit_approximation"].all()
    assert not pit["production_reconciled"].any()
    assert status["is_suspended"].any()
    assert status["is_limit_up"].any()
    assert not status["is_st_known"].any()
    assert set(actions["action_type"]) == {"free_adjustment_column_audit"}
    assert not actions["production_reconciled"].any()


def test_v31_quality_gate_is_research_ready_but_not_production_ready(tmp_path) -> None:
    validate = _load_module("validate_v31_free_data_quality_gate")
    output_dir, corporate_action_path = _build_sample_outputs(tmp_path)
    build_report = tmp_path / "build_report.json"
    build_report.write_text(
        '{"summary": {"symbols_loaded": 2, "unique_trading_dates": 4}, "failure_count": 0}',
        encoding="utf-8",
    )

    report = validate.build_report(
        pit_path=output_dir / "pit_universe_v31.parquet",
        status_path=output_dir / "trading_status_v31.parquet",
        corporate_action_path=corporate_action_path,
        build_report_path=build_report,
        min_entities=2,
        min_dates=4,
        min_corporate_action_entities=2,
        generated_at=datetime(2026, 6, 18, tzinfo=timezone.utc),
    )

    assert report["free_research_ready"] is True
    assert report["production_data_ready"] is False
    assert report["summary"]["research_ready_count"] == 3
    assert any("V30 production-data gate" in blocker for blocker in report["production_blockers"])


def test_v31_summary_is_visible_in_strategy_readiness(tmp_path) -> None:
    score = _load_module("score_strategy_readiness_v27")
    gate = tmp_path / "gate.json"
    gate.write_text(
        """{
          "free_research_ready": true,
          "production_data_ready": false,
          "summary": {
            "research_ready_count": 3,
            "total_requirements": 3,
            "research_blocker_count": 0,
            "production_blocker_count": 5,
            "build_symbols_loaded": 2000,
            "build_unique_trading_dates": 4954
          },
          "research_blockers": [],
          "production_blockers": ["not production"]
        }""",
        encoding="utf-8",
    )

    summary = score._v31_free_data_quality_gate_summary(gate)

    assert summary["exists"] is True
    assert summary["free_research_ready"] is True
    assert summary["production_data_ready"] is False
    assert summary["build_symbols_loaded"] == 2000
