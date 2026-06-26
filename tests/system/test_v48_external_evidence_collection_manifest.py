import importlib.util
import sys
from datetime import datetime, timezone
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


def test_v48_manifest_includes_all_v44_and_v30_requirements() -> None:
    module = _load_module("build_v48_external_evidence_collection_manifest")

    manifest = module.build_manifest(
        v48_gate={
            "evidence_bundle_valid": False,
            "missing_external_ref_keys": ["external_worm_archive", "paper_trading_90d"],
            "missing_production_data_ref_keys": ["pit_security_master"],
            "v44_external_refs": {},
            "production_data_refs": {},
        },
        paper_health={
            "progress": {
                "report_days": 2,
                "calendar_days": 2,
                "paper_evidence_threshold_met": False,
            }
        },
        generated_at=datetime(2026, 6, 26, tzinfo=timezone.utc),
    )

    keys = {item["key"] for item in manifest["items"]}
    assert "external_worm_archive" in keys
    assert "paper_trading_90d" in keys
    assert "pit_security_master" in keys
    assert manifest["production_ready"] is False
    assert manifest["summary"]["paper_report_days"] == 2
    assert manifest["summary"]["total_items"] == 19


def test_v48_manifest_preserves_fail_closed_status_for_missing_refs() -> None:
    module = _load_module("build_v48_external_evidence_collection_manifest")

    manifest = module.build_manifest(
        v48_gate={
            "evidence_bundle_valid": False,
            "missing_external_ref_keys": ["paper_trading_90d"],
            "missing_production_data_ref_keys": [],
            "v44_external_refs": {"external_worm_archive": "worm://prod/attestation/1"},
            "production_data_refs": {"pit_security_master": "security-master://vendor/run/1"},
        },
        paper_health={"progress": {"report_days": 2}},
        generated_at=datetime(2026, 6, 26, tzinfo=timezone.utc),
    )

    items = {item["key"]: item for item in manifest["items"]}
    assert items["external_worm_archive"]["current_status"] == "provided_pending_gate_validation"
    assert items["paper_trading_90d"]["current_status"] == "missing"
    assert items["paper_trading_90d"]["paper_progress"]["report_days"] == 2
    assert manifest["source_gate_valid"] is False
    assert manifest["evidence_collection_complete"] is False


def test_v48_manifest_orders_vendor_data_before_runtime_evidence() -> None:
    module = _load_module("build_v48_external_evidence_collection_manifest")

    manifest = module.build_manifest(
        v48_gate={
            "evidence_bundle_valid": False,
            "missing_external_ref_keys": [],
            "missing_production_data_ref_keys": [],
            "v44_external_refs": {},
            "production_data_refs": {},
        },
        paper_health={},
        generated_at=datetime(2026, 6, 26, tzinfo=timezone.utc),
    )

    ordered_keys = [item["key"] for item in manifest["items"]]
    assert ordered_keys[:3] == [
        "pit_security_master",
        "trading_status_actions",
        "corporate_action_reconciliation",
    ]
    assert ordered_keys.index("paper_trading_90d") > ordered_keys.index("external_worm_archive")
