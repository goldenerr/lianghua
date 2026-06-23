import importlib.util
import json
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def _load_module():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "validate_v48_external_evidence_bundle",
        SCRIPTS / "validate_v48_external_evidence_bundle.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _complete_bundle() -> dict:
    return {
        "version": "V48-production-external-evidence-bundle",
        "external_refs": {
            "external_worm_archive": "worm://archive/audit-chain/2026-06-23",
            "secret_manager": "vault://kv/quant/prod/accounts",
            "approval_service": "approval://risk-board/v44/full-config-hash",
            "real_market_data_provider": "provider://vendor/a-share/pit-feed-2026",
            "provider_entitlement": "entitlement://vendor/a-share/contract-2026",
            "alt_archive_readiness": "artifact://alt-archive/v44/2026-06-23",
            "exchange_position_provider": "broker://prod/reconciled-position-api",
            "broker_borrow_availability": "borrow-feed://broker-prod/a-share/shortable-feed",
            "paper_trading_90d": "paper://v40-shadow/2026-03-01_2026-05-31",
            "signed_plugin_review": "signature://plugins/v40/reviewed",
            "production_calendar": "calendar://XSHG/vendor-approved/2026",
            "rollover_runbook": "runbook://futures-rollover/manual-approval/v1",
            "capacity_benchmark": "benchmark://prod-like/v44/2026-06",
            "failover_dr_test": "dr-test://multi-region/v44/2026-06",
        },
        "production_data_refs": {
            "pit_security_master": "security-master://vendor/a-share/pit/2006-2026",
            "trading_status_actions": "trading-status://vendor/a-share/status/2006-2026",
            "corporate_action_reconciliation": "reconciliation://vendor/a-share/actions/2006-2026",
            "real_tick_minute_execution_feed": "minute-feed://vendor/a-share/minute/2006-2026",
            "exchange_order_fill_replay": "broker-fill://prod-paper/order-fills/2026",
        },
    }


def test_missing_bundle_fails_closed_and_template_is_non_secret() -> None:
    module = _load_module()

    template = module.build_template()
    payload = module.validate_bundle({})

    assert template["template_only"] is True
    assert template["production_ready"] is False
    assert set(template["external_refs"]) == set(payload["required_external_ref_keys"])
    assert set(template["production_data_refs"]) == set(payload["required_production_data_ref_keys"])
    assert all(value == "" for value in template["external_refs"].values())
    assert all(value == "" for value in template["production_data_refs"].values())
    assert "Bearer " not in json.dumps(template, ensure_ascii=False)
    assert "sk-" not in json.dumps(template, ensure_ascii=False)

    assert payload["production_ready"] is False
    assert payload["evidence_bundle_valid"] is False
    assert payload["missing_external_ref_keys"] == payload["required_external_ref_keys"]
    assert payload["missing_production_data_ref_keys"] == payload["required_production_data_ref_keys"]


def test_complete_bundle_validates_but_never_sets_production_ready() -> None:
    module = _load_module()

    payload = module.validate_bundle(_complete_bundle())

    assert payload["evidence_bundle_valid"] is True
    assert payload["production_ready"] is False
    assert payload["blockers"] == []
    assert payload["missing_external_ref_keys"] == []
    assert payload["missing_production_data_ref_keys"] == []
    assert all(payload["scope_status"].values())


def test_sensitive_and_placeholder_refs_are_rejected() -> None:
    module = _load_module()
    bundle = _complete_bundle()
    bundle["api_key"] = "Bearer should-not-be-here"
    bundle["external_refs"]["capacity_benchmark"] = "benchmark://pending"
    bundle["production_data_refs"]["real_tick_minute_execution_feed"] = "local://minute"

    payload = module.validate_bundle(bundle)

    assert payload["evidence_bundle_valid"] is False
    assert payload["production_ready"] is False
    assert payload["scope_status"]["sensitive_material_free"] is False
    assert any("疑似敏感字段" in blocker for blocker in payload["blockers"])
    assert any("疑似敏感值" in blocker for blocker in payload["blockers"])
    assert any("capacity_benchmark" in blocker for blocker in payload["blockers"])
    assert any("real_tick_minute_execution_feed" in blocker for blocker in payload["blockers"])


def test_cli_writes_v44_refs_only_when_bundle_valid(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle.json"
    output_path = tmp_path / "gate.json"
    report_path = tmp_path / "gate.md"
    refs_path = tmp_path / "v44_refs.json"
    bundle_path.write_text(json.dumps(_complete_bundle(), ensure_ascii=False), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "validate_v48_external_evidence_bundle.py"),
            "--evidence-bundle-json",
            str(bundle_path),
            "--output-json",
            str(output_path),
            "--report-md",
            str(report_path),
            "--v44-evidence-refs-json",
            str(refs_path),
            "--write-v44-evidence-refs",
            "--require-bundle-valid",
        ],
        check=True,
        cwd=PROJECT,
        capture_output=True,
        text=True,
    )

    assert "evidence_bundle_valid" in result.stdout
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    refs = json.loads(refs_path.read_text(encoding="utf-8"))
    assert payload["evidence_bundle_valid"] is True
    assert payload["production_ready"] is False
    assert refs == _complete_bundle()["external_refs"]
    assert "哪些需要人工/外部系统提供" in report_path.read_text(encoding="utf-8")


def test_report_is_chinese_and_keeps_external_work_explicit(tmp_path: Path) -> None:
    module = _load_module()
    report_path = tmp_path / "report.md"
    payload = module.validate_bundle({})

    module._write_report(report_path, payload)

    report = report_path.read_text(encoding="utf-8")
    assert "哪些需要人工/外部系统提供" in report
    assert "不能本地伪造" in report
    assert "我已经能自动处理的部分" in report
