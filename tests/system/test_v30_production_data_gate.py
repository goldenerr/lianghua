import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def _load_module():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "validate_v30_production_data_gate",
        SCRIPTS / "validate_v30_production_data_gate.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _passing_refs() -> dict[str, str]:
    return {
        "pit_security_master": "security-master://vendor/a-share/pit/2006-2026",
        "trading_status_actions": "trading-status://vendor/a-share/status/2006-2026",
        "corporate_action_reconciliation": "reconciliation://vendor/a-share/actions/2006-2026",
        "real_tick_minute_execution_feed": "minute-feed://vendor/a-share/minute/2006-2026",
        "exchange_order_fill_replay": "broker-fill://prod-paper/order-fills/2026",
    }


def _gate_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    audit = tmp_path / "audit20y.json"
    external = tmp_path / "external.json"
    pit = tmp_path / "pit.json"
    audit.write_text(
        """{
          "assessment": {
            "can_claim_20y_full_universe_production_validation": true,
            "can_run_20y_diagnostic_backtest": true,
            "diagnostic_start_after_warmup": "2006-01-01",
            "production_full_universe_start_after_warmup": "2006-01-01",
            "minimum_production_symbols": 1900
          }
        }""",
        encoding="utf-8",
    )
    external.write_text('{"production_ready": true, "missing_or_untrusted_blockers": []}', encoding="utf-8")
    pit.write_text(
        '{"production_data_ready": true, "summary": {"research_ready_panels": 10, "total_panels": 10}}',
        encoding="utf-8",
    )
    return audit, external, pit


def _write_requirement_file(path: Path, *, date_column: str, columns: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    dates = pd.date_range("2024-01-01", periods=3)
    rows = []
    for date in dates:
        for code in ["000001", "000002"]:
            row = {column: "ok" for column in columns}
            row[date_column] = date
            if "timestamp" in columns:
                row["timestamp"] = pd.Timestamp(date).tz_localize("UTC")
            if "code" in columns:
                row["code"] = code
            rows.append(row)
    pd.DataFrame(rows).to_parquet(path)


def _small_requirements(module, tmp_path: Path):
    base = tmp_path / "data"
    return (
        module.DataRequirement(
            "pit_security_master",
            "真正 PIT 历史股票池",
            base / "pit.parquet",
            ("date", "code"),
            "date",
            "code",
            3,
            2,
            "pit_security_master",
            "缺 PIT",
        ),
        module.DataRequirement(
            "trading_status_actions",
            "交易状态",
            base / "status.parquet",
            ("date", "code"),
            "date",
            "code",
            3,
            2,
            "trading_status_actions",
            "缺交易状态",
        ),
        module.DataRequirement(
            "corporate_action_reconciliation",
            "复权复核",
            base / "actions.parquet",
            ("date", "code"),
            "date",
            "code",
            3,
            2,
            "corporate_action_reconciliation",
            "缺复权复核",
        ),
        module.DataRequirement(
            "real_tick_minute_execution_feed",
            "分钟执行",
            base / "minute.parquet",
            ("timestamp", "code"),
            "timestamp",
            "code",
            3,
            2,
            "real_tick_minute_execution_feed",
            "缺分钟执行",
        ),
        module.DataRequirement(
            "exchange_order_fill_replay",
            "成交回放",
            base / "fills.parquet",
            ("timestamp", "code"),
            "timestamp",
            "code",
            3,
            2,
            "exchange_order_fill_replay",
            "缺成交回放",
        ),
    )


def test_v30_gate_fails_closed_with_missing_local_files_and_refs(monkeypatch, tmp_path) -> None:
    module = _load_module()
    audit, external, pit = _gate_files(tmp_path)
    monkeypatch.setattr(module, "_data_requirements", lambda: _small_requirements(module, tmp_path))
    monkeypatch.setattr(module, "_summarize_existing_intraday", lambda: {"exists": False})
    monkeypatch.setattr(module, "_audit_price_adjustment_columns", lambda: {"sampled_files": 0})

    report = module.build_report(
        evidence={},
        audit_20y_path=audit,
        external_gate_path=external,
        pit_panel_path=pit,
        generated_at=datetime(2026, 6, 17, tzinfo=timezone.utc),
    )

    assert report["production_data_ready"] is False
    assert report["summary"]["production_ready_count"] == 0
    assert any("缺 PIT" in blocker for blocker in report["blockers"])
    assert any("pit_security_master" in blocker for blocker in report["blockers"])


def test_v30_gate_passes_only_with_local_coverage_and_external_refs(monkeypatch, tmp_path) -> None:
    module = _load_module()
    requirements = _small_requirements(module, tmp_path)
    for requirement in requirements:
        _write_requirement_file(
            requirement.local_path,
            date_column=requirement.date_column or "date",
            columns=requirement.required_columns,
        )
    audit, external, pit = _gate_files(tmp_path)
    monkeypatch.setattr(module, "_data_requirements", lambda: requirements)
    monkeypatch.setattr(module, "_summarize_existing_intraday", lambda: {"exists": True})
    monkeypatch.setattr(module, "_audit_price_adjustment_columns", lambda: {"sampled_files": 1})

    report = module.build_report(
        evidence=_passing_refs(),
        audit_20y_path=audit,
        external_gate_path=external,
        pit_panel_path=pit,
        generated_at=datetime(2026, 6, 17, tzinfo=timezone.utc),
    )

    assert report["production_data_ready"] is True
    assert report["summary"]["production_ready_count"] == 5
    assert report["blockers"] == []


def test_v30_gate_rejects_placeholder_or_local_evidence(monkeypatch, tmp_path) -> None:
    module = _load_module()
    requirements = _small_requirements(module, tmp_path)
    for requirement in requirements:
        _write_requirement_file(
            requirement.local_path,
            date_column=requirement.date_column or "date",
            columns=requirement.required_columns,
        )
    audit, external, pit = _gate_files(tmp_path)
    evidence = _passing_refs()
    evidence["real_tick_minute_execution_feed"] = "local://minute"
    evidence["exchange_order_fill_replay"] = "broker-fill://pending"
    monkeypatch.setattr(module, "_data_requirements", lambda: requirements)
    monkeypatch.setattr(module, "_summarize_existing_intraday", lambda: {"exists": True})
    monkeypatch.setattr(module, "_audit_price_adjustment_columns", lambda: {"sampled_files": 1})

    report = module.build_report(
        evidence=evidence,
        audit_20y_path=audit,
        external_gate_path=external,
        pit_panel_path=pit,
        generated_at=datetime(2026, 6, 17, tzinfo=timezone.utc),
    )

    assert report["production_data_ready"] is False
    assert any("placeholder/local" in blocker for blocker in report["blockers"])


def test_v30_load_evidence_merges_direct_env_refs(monkeypatch, tmp_path) -> None:
    module = _load_module()
    evidence_file = tmp_path / "evidence.json"
    evidence_file.write_text('{"pit_security_master": "security-master://file/ref"}', encoding="utf-8")
    monkeypatch.setenv(
        "QUANT_EVIDENCE_PIT_SECURITY_MASTER_REF",
        "security-master://env/ref",
    )

    evidence = module._load_evidence(str(evidence_file))

    assert evidence["pit_security_master"] == "security-master://env/ref"
