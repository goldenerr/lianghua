import importlib.util
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def _load_module():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "validate_v42_paper_shadow_evidence",
        SCRIPTS / "validate_v42_paper_shadow_evidence.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _package(blockers: list[str] | None = None) -> dict:
    return {
        "shadow_candidate_exists": True,
        "paper_shadow_ready": not blockers,
        "production_ready": False,
        "candidate": {
            "auto_trade_enabled": False,
            "live_order_submission_allowed": False,
            "max_live_capital_fraction": 0.0,
        },
        "blockers": blockers or [],
    }


def _ledger(days: int = 90) -> dict:
    start = date(2026, 1, 2)
    reports = []
    for offset in range(days):
        current = start + timedelta(days=offset)
        reports.append(
            {
                "date": current.isoformat(),
                "paper_daily_return": 0.0010,
                "backtest_daily_return": 0.0011,
                "actual_slippage_bps": 2.0,
                "expected_slippage_bps": 3.0,
                "actual_cost_bps": 4.0,
                "expected_cost_bps": 5.0,
                "risk_capacity_violations": 0,
                "invariant_violations": 0,
                "audit_hash": "a" * 64,
                "audit_event_count": 9,
                "archive_ref": f"worm://paper-shadow/v40/{current.isoformat()}",
                "market_data_ref": f"provider://paper-shadow/feed/{current.isoformat()}",
                "position_snapshot_ref": f"broker://paper-shadow/position/{current.isoformat()}",
                "order_fill_log_ref": f"broker://paper-shadow/fills/{current.isoformat()}",
                "auto_trade_enabled": False,
                "live_order_submission_allowed": False,
            }
        )
    return {
        "external_refs": {"paper_trading_90d": "paper://v40-shadow/2026-q1"},
        "daily_reports": reports,
    }


def test_gate_fails_closed_without_daily_ledger() -> None:
    module = _load_module()

    payload = module.build_evidence_gate(
        _package(blockers=["robustness failed"]),
        {},
        generated_at=datetime(2026, 6, 23, tzinfo=timezone.utc),
    )

    assert payload["paper_evidence_ready"] is False
    assert payload["production_ready"] is False
    assert payload["package_blocker_count"] == 1
    assert any("daily paper-shadow reports" in item for item in payload["blockers"])


def test_gate_passes_only_for_complete_paper_shadow_evidence() -> None:
    module = _load_module()

    payload = module.build_evidence_gate(
        _package(),
        _ledger(),
        generated_at=datetime(2026, 6, 23, tzinfo=timezone.utc),
    )

    assert payload["paper_evidence_ready"] is True
    assert payload["production_ready"] is False
    assert payload["metrics"]["report_days"] == 90
    assert payload["metrics"]["calendar_days"] == 90
    assert payload["metrics"]["mean_abs_daily_return_drift_bps"] == 1.0
    assert payload["blockers"] == []


def test_gate_rejects_live_permission_and_sensitive_material() -> None:
    module = _load_module()
    ledger = _ledger()
    ledger["daily_reports"][0]["live_order_submission_allowed"] = True
    ledger["api_key"] = "sk-abcdefghijkl"

    payload = module.build_evidence_gate(
        _package(),
        ledger,
        generated_at=datetime(2026, 6, 23, tzinfo=timezone.utc),
    )

    assert payload["paper_evidence_ready"] is False
    assert any("live_order_submission_allowed" in item for item in payload["blockers"])
    assert any("sensitive-looking key" in item for item in payload["blockers"])
    assert any("sensitive-looking value" in item for item in payload["blockers"])
