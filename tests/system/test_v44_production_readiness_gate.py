import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def _load_module():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "prepare_v44_production_readiness_gate",
        SCRIPTS / "prepare_v44_production_readiness_gate.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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


def _v42_ready() -> dict:
    return {
        "paper_evidence_ready": True,
        "production_ready": False,
        "blockers": [],
    }


def _v43_ready() -> dict:
    return {
        "local_capacity_dr_gate_passes": True,
        "production_ready": False,
        "production_like": False,
        "local_blockers": [],
    }


def _complete_external_evidence() -> dict[str, str]:
    return {
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
    }


def test_external_evidence_alone_does_not_bypass_strategy_and_paper_gates() -> None:
    module = _load_module()
    v40 = _v40_ready()
    v40["paper_shadow_ready"] = False
    v40["blockers"] = ["robustness_gate_failed"]
    v42 = _v42_ready()
    v42["paper_evidence_ready"] = False
    v42["blockers"] = ["missing daily paper-shadow reports"]

    payload = module.build_readiness_gate(
        v40_package=v40,
        v42_gate=v42,
        v43_gate=_v43_ready(),
        evidence=_complete_external_evidence(),
        generated_at=datetime(2026, 6, 23, tzinfo=timezone.utc),
    )

    assert payload["production_ready"] is False
    assert payload["scope_status"]["external_evidence_passes"] is True
    assert payload["scope_status"]["candidate_package_passes"] is False
    assert payload["scope_status"]["paper_evidence_passes"] is False
    assert any("V40 影子模拟盘包未就绪" in item for item in payload["production_blockers"])
    assert any("V42 影子模拟盘" in item for item in payload["production_blockers"])


def test_placeholder_or_local_external_evidence_remains_blocking() -> None:
    module = _load_module()
    evidence = _complete_external_evidence()
    evidence["capacity_benchmark"] = "benchmark://pending"
    evidence["failover_dr_test"] = "local://dr-test"

    payload = module.build_readiness_gate(
        v40_package=_v40_ready(),
        v42_gate=_v42_ready(),
        v43_gate=_v43_ready(),
        evidence=evidence,
        generated_at=datetime(2026, 6, 23, tzinfo=timezone.utc),
    )

    assert payload["production_ready"] is False
    assert payload["scope_status"]["external_evidence_passes"] is False
    assert any("capacity_benchmark" in item for item in payload["external_evidence_blockers"])
    assert any("failover_dr_test" in item for item in payload["external_evidence_blockers"])


def test_all_child_gates_and_external_evidence_are_required_for_ready() -> None:
    module = _load_module()

    payload = module.build_readiness_gate(
        v40_package=_v40_ready(),
        v42_gate=_v42_ready(),
        v43_gate=_v43_ready(),
        evidence=_complete_external_evidence(),
        generated_at=datetime(2026, 6, 23, tzinfo=timezone.utc),
    )

    assert payload["production_ready"] is True
    assert payload["production_blockers"] == []
    assert all(payload["scope_status"].values())
