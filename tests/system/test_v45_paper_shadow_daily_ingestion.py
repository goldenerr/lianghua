import importlib.util
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def _load_module():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "ingest_v45_paper_shadow_daily_report",
        SCRIPTS / "ingest_v45_paper_shadow_daily_report.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _daily(date: str = "2026-01-02") -> dict:
    return {
        "external_refs": {"paper_trading_90d": "paper://v40-shadow/2026-q1"},
        "daily_report": {
            "date": date,
            "paper_daily_return": 0.001,
            "backtest_daily_return": 0.0011,
            "actual_slippage_bps": 2.0,
            "expected_slippage_bps": 3.0,
            "actual_cost_bps": 4.0,
            "expected_cost_bps": 5.0,
            "risk_capacity_violations": 0,
            "invariant_violations": 0,
            "audit_events": [{"event_type": "paper_order"}, {"event_type": "paper_fill"}],
            "archive_ref": f"worm://paper-shadow/{date}",
            "market_data_ref": f"provider://paper-shadow/feed/{date}",
            "position_snapshot_ref": f"broker://paper-shadow/position/{date}",
            "order_fill_log_ref": f"broker://paper-shadow/fills/{date}",
            "auto_trade_enabled": False,
            "live_order_submission_allowed": False,
        },
    }


def _v40_ready() -> dict:
    return {
        "shadow_candidate_exists": True,
        "paper_shadow_ready": True,
        "production_ready": False,
        "candidate": {
            "capital": 200000,
            "stock_alpha_cap": 0.07,
            "crisis_cap": 0.20,
            "stock_rebalance_freq": 40,
            "auto_trade_enabled": False,
            "live_order_submission_allowed": False,
            "max_live_capital_fraction": 0.0,
        },
        "blockers": [],
    }


def _v43_ready() -> dict:
    return {
        "production_ready": False,
        "production_like": False,
        "local_capacity_dr_gate_passes": True,
        "local_blockers": [],
    }


def test_canonicalize_daily_report_computes_audit_hash_and_blocks_live_flags() -> None:
    module = _load_module()
    row, warnings = module.canonicalize_daily_report(_daily())

    assert row["date"] == "2026-01-02"
    assert len(row["audit_hash"]) == 64
    assert row["audit_event_count"] == 2
    assert row["auto_trade_enabled"] is False
    assert warnings == []

    bad = _daily()
    bad["daily_report"]["live_order_submission_allowed"] = True
    try:
        module.canonicalize_daily_report(bad)
    except ValueError as exc:
        assert "实盘下单" in str(exc)
    else:
        raise AssertionError("live order permission must be rejected")


def test_sensitive_material_is_rejected_before_ledger_write() -> None:
    module = _load_module()
    bad = _daily()
    bad["daily_report"]["api_key"] = "sk-abcdefghijkl"

    try:
        module.canonicalize_daily_report(bad)
    except ValueError as exc:
        assert "敏感字段" in str(exc)
        assert "敏感值" in str(exc)
    else:
        raise AssertionError("sensitive daily report must be rejected")


def test_upsert_rejects_changed_duplicate_without_replace() -> None:
    module = _load_module()
    ledger = module.default_ledger()
    row, _ = module.canonicalize_daily_report(_daily())
    assert module.upsert_daily_report(ledger, row) == "inserted"

    changed, _ = module.canonicalize_daily_report(_daily())
    changed["paper_daily_return"] = 0.002
    changed["report_sha256"] = "b" * 64

    try:
        module.upsert_daily_report(ledger, changed)
    except ValueError as exc:
        assert "--allow-replace" in str(exc)
    else:
        raise AssertionError("changed duplicate must require explicit replacement")

    assert module.upsert_daily_report(ledger, changed, allow_replace=True) == "replaced"
    assert ledger["daily_reports"][0]["paper_daily_return"] == 0.002


def test_run_ingestion_refreshes_v42_and_v44_fail_closed(tmp_path: Path) -> None:
    module = _load_module()
    daily_path = tmp_path / "daily.json"
    daily_path.write_text(json.dumps(_daily(), ensure_ascii=False), encoding="utf-8")
    v40_path = tmp_path / "v40.json"
    v40_path.write_text(json.dumps(_v40_ready(), ensure_ascii=False), encoding="utf-8")
    v43_path = tmp_path / "v43.json"
    v43_path.write_text(json.dumps(_v43_ready(), ensure_ascii=False), encoding="utf-8")

    summary = module.run_ingestion(
        daily_report_path=daily_path,
        allow_replace=False,
        ledger_path=tmp_path / "ledger.json",
        summary_path=tmp_path / "summary.json",
        report_path=tmp_path / "report.md",
        v40_package_path=v40_path,
        v42_output_path=tmp_path / "v42.json",
        v42_report_path=tmp_path / "v42.md",
        v43_gate_path=v43_path,
        v44_output_path=tmp_path / "v44.json",
        v44_report_path=tmp_path / "v44.md",
        evidence={},
    )

    assert summary["daily_report_count"] == 1
    assert summary["ingestion_action"] == "inserted"
    assert summary["v42_paper_evidence_ready"] is False
    assert summary["v44_production_ready"] is False
    ledger = json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))
    assert ledger["external_refs"]["paper_trading_90d"] == "paper://v40-shadow/2026-q1"
    assert json.loads((tmp_path / "v42.json").read_text(encoding="utf-8"))["production_ready"] is False
    assert json.loads((tmp_path / "v44.json").read_text(encoding="utf-8"))["production_ready"] is False
