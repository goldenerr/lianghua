import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def _load_module():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "prepare_v29_paper_launch_package",
        SCRIPTS / "prepare_v29_paper_launch_package.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _readiness() -> dict:
    return {
        "strategy_gate_passes": True,
        "industry_leading_candidate_exists": True,
        "recommended_candidate": {
            "name": "v29_price_meta_lowvol_recent_guard",
            "score": 126.1,
            "full_sharpe": 2.25,
            "avg_oos_sharpe": 2.48,
            "min_oos_sharpe": 0.17,
            "max_drawdown": -0.096,
            "win_rate": 0.57,
            "production_minimum_passes": True,
            "industry_leading_passes": True,
        },
    }


def _robustness() -> dict:
    return {
        "candidate": "v29_price_meta_lowvol_recent_guard",
        "robustness_gate_passes": True,
    }


def _research() -> dict:
    return {"results": [{"name": "v29_price_meta_lowvol_recent_guard"}]}


def _paper_start_evidence() -> dict[str, str]:
    return {
        "external_worm_archive": "worm://archive/audit-chain/2026-06-16",
        "secret_manager": "vault://kv/quant/prod/accounts",
        "approval_service": "approval://risk-board/v29/config-hash",
        "real_market_data_provider": "provider://vendor/a-share/price-feed-2026",
        "provider_entitlement": "entitlement://vendor/a-share/contract-2026",
        "exchange_position_provider": "broker://prod/reconciled-position-api",
        "signed_plugin_review": "signature://plugins/v29/reviewed",
        "production_calendar": "calendar://XSHG/vendor-approved/2026",
        "capacity_benchmark": "benchmark://prod-like/v29/2026-06",
    }


def test_v29_launch_package_fails_closed_without_external_evidence() -> None:
    module = _load_module()

    package = module.build_launch_package(
        readiness=_readiness(),
        research=_research(),
        robustness=_robustness(),
        external_gate={"production_ready": False},
        evidence={},
        artifact_hashes={},
        generated_at=datetime(2026, 6, 16, tzinfo=timezone.utc),
    )

    assert package["paper_launch_ready"] is False
    assert package["production_ready"] is False
    assert package["launch_mode"] == "blocked"
    assert any("external_worm_archive" in item for item in package["paper_launch_blockers"])
    assert any("secret_manager" in item for item in package["paper_launch_blockers"])


def test_v29_launch_package_allows_paper_only_when_candidate_scoped_refs_exist() -> None:
    module = _load_module()

    package = module.build_launch_package(
        readiness=_readiness(),
        research=_research(),
        robustness=_robustness(),
        external_gate={"production_ready": False},
        evidence=_paper_start_evidence(),
        artifact_hashes={"readiness_json_sha256": "abc"},
        generated_at=datetime(2026, 6, 16, tzinfo=timezone.utc),
    )

    assert package["paper_launch_ready"] is True
    assert package["production_ready"] is False
    assert package["launch_mode"] == "approved_90d_paper_trading"
    assert "broker_borrow_availability" in package["missing_evidence_for_full_production"]
    assert "paper_trading_90d" in package["missing_evidence_for_full_production"]
    assert package["candidate_scope"]["uses_short_or_borrow"] is False


def test_v29_launch_runbook_does_not_render_secret_values_or_evidence_refs() -> None:
    module = _load_module()
    evidence = _paper_start_evidence()
    evidence["secret_manager"] = "vault://kv/prod/sensitive-path"

    package = module.build_launch_package(
        readiness=_readiness(),
        research=_research(),
        robustness=_robustness(),
        external_gate={"production_ready": False},
        evidence=evidence,
        artifact_hashes={},
        generated_at=datetime(2026, 6, 16, tzinfo=timezone.utc),
    )
    runbook = module.render_runbook(package)

    assert "vault://kv/prod/sensitive-path" not in runbook
    assert "QUANT_SECRET_MANAGER_TOKEN_REF" in runbook
    assert "API Key" in runbook
    assert "状态：由启动包生成" in runbook
