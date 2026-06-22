import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def _load_module():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "collect_v29_external_evidence_refs",
        SCRIPTS / "collect_v29_external_evidence_refs.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _paper_env_refs() -> dict[str, str]:
    return {
        "QUANT_EVIDENCE_EXTERNAL_WORM_ARCHIVE_REF": "worm://archive/v29/20260616",
        "QUANT_EVIDENCE_SECRET_MANAGER_REF": "vault://kv/quant/prod/accounts",
        "QUANT_EVIDENCE_APPROVAL_SERVICE_REF": "approval://risk/v29/config-hash",
        "QUANT_EVIDENCE_REAL_MARKET_DATA_PROVIDER_REF": "provider://vendor/a-share/price-feed-2026",
        "QUANT_EVIDENCE_PROVIDER_ENTITLEMENT_REF": "entitlement://vendor/a-share/contract-2026",
        "QUANT_EVIDENCE_EXCHANGE_POSITION_PROVIDER_REF": "broker://prod/reconciled-position-api",
        "QUANT_EVIDENCE_SIGNED_PLUGIN_REVIEW_REF": "signature://plugins/v29/reviewed",
        "QUANT_EVIDENCE_PRODUCTION_CALENDAR_REF": "calendar://XSHG/vendor-approved/2026",
        "QUANT_EVIDENCE_CAPACITY_BENCHMARK_REF": "benchmark://prod-like/v29/2026-06",
    }


def test_collector_env_refs_make_v29_paper_ready_but_not_full_production() -> None:
    module = _load_module()

    evidence = module.collect_direct_refs({}, env=_paper_env_refs())
    report = module.build_collection_report(
        evidence,
        generated_at=datetime(2026, 6, 16, tzinfo=timezone.utc),
    )

    assert report["paper_launch_evidence_ready"] is True
    assert report["production_evidence_ready"] is False
    assert report["paper_launch_blockers"] == []
    assert "broker_borrow_availability" in report["missing_for_full_production"]
    assert "paper_trading_90d" in report["missing_for_full_production"]


def test_collector_rejects_placeholder_and_local_refs() -> None:
    module = _load_module()
    env = _paper_env_refs()
    env["QUANT_EVIDENCE_REAL_MARKET_DATA_PROVIDER_REF"] = "local-mock://provider"
    env["QUANT_EVIDENCE_CAPACITY_BENCHMARK_REF"] = "benchmark://pending"

    evidence = module.collect_direct_refs({}, env=env)
    report = module.build_collection_report(
        evidence,
        generated_at=datetime(2026, 6, 16, tzinfo=timezone.utc),
    )

    assert report["paper_launch_evidence_ready"] is False
    assert any("real_market_data_provider" in item for item in report["paper_launch_blockers"])
    assert any("capacity_benchmark" in item for item in report["paper_launch_blockers"])


def test_collector_report_and_outputs_do_not_contain_tokens(tmp_path) -> None:
    module = _load_module()
    env = _paper_env_refs()
    env["QUANT_SECRET_MANAGER_TOKEN"] = "super-secret-token"
    evidence = module.collect_direct_refs({}, env=env)
    report = module.build_collection_report(
        evidence,
        generated_at=datetime(2026, 6, 16, tzinfo=timezone.utc),
    )
    evidence_path = tmp_path / "evidence.json"
    report_path = tmp_path / "report.json"

    module.write_outputs(
        evidence,
        report,
        output_evidence_path=evidence_path,
        output_report_path=report_path,
    )

    assert "super-secret-token" not in evidence_path.read_text(encoding="utf-8")
    assert "super-secret-token" not in report_path.read_text(encoding="utf-8")
