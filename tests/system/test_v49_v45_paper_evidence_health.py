import importlib.util
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def _load_module(name: str):
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_runtime(tmp_path: Path, dates: list[str]) -> tuple[Path, Path, Path]:
    ledger = tmp_path / "quant_v45_paper_shadow_ledger.json"
    reports = tmp_path / "daily_reports"
    exports = tmp_path / "source_exports"
    reports.mkdir()
    exports.mkdir()
    rows = []
    for report_date in dates:
        (reports / f"{report_date}.json").write_text("{}", encoding="utf-8")
        (exports / f"{report_date}.json").write_text("{}", encoding="utf-8")
        rows.append(
            {
                "date": report_date,
                "paper_daily_return": 0.0,
                "backtest_daily_return": 0.0,
                "risk_capacity_violations": 0,
                "invariant_violations": 0,
                "auto_trade_enabled": False,
                "live_order_submission_allowed": False,
            }
        )
    ledger.write_text(json.dumps({"daily_reports": rows}), encoding="utf-8")
    return ledger, reports, exports


def test_paper_health_reports_progress_without_treating_immaturity_as_blocker(tmp_path) -> None:
    module = _load_module("check_v49_v45_paper_evidence_health")
    ledger, reports, exports = _write_runtime(tmp_path, ["2026-06-24", "2026-06-25"])

    health = module.build_health_report(
        ledger_path=ledger,
        daily_report_dir=reports,
        source_export_dir=exports,
        as_of=date(2026, 6, 26),
        max_stale_calendar_days=5,
        min_calendar_days=90,
        min_report_days=60,
        generated_at=datetime(2026, 6, 26, tzinfo=timezone.utc),
    )

    assert health["health_status"] == "ok"
    assert health["progress"]["report_days"] == 2
    assert health["progress"]["paper_evidence_threshold_met"] is False
    assert health["blockers"] == []


def test_paper_health_blocks_stale_ledger(tmp_path) -> None:
    module = _load_module("check_v49_v45_paper_evidence_health")
    ledger, reports, exports = _write_runtime(tmp_path, ["2026-06-24"])

    health = module.build_health_report(
        ledger_path=ledger,
        daily_report_dir=reports,
        source_export_dir=exports,
        as_of=date(2026, 7, 10),
        max_stale_calendar_days=5,
        min_calendar_days=90,
        min_report_days=60,
        generated_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
    )

    assert health["health_status"] == "blocked"
    assert any("stale" in blocker for blocker in health["blockers"])


def test_paper_health_blocks_missing_runtime_files(tmp_path) -> None:
    module = _load_module("check_v49_v45_paper_evidence_health")
    ledger, reports, exports = _write_runtime(tmp_path, ["2026-06-24"])
    (reports / "2026-06-24.json").unlink()

    health = module.build_health_report(
        ledger_path=ledger,
        daily_report_dir=reports,
        source_export_dir=exports,
        as_of=date(2026, 6, 25),
        max_stale_calendar_days=5,
        min_calendar_days=90,
        min_report_days=60,
        generated_at=datetime(2026, 6, 25, tzinfo=timezone.utc),
    )

    assert health["health_status"] == "blocked"
    assert any("missing runtime daily report file" in blocker for blocker in health["blockers"])
