import pytest
from quant_trading.core.audit import AuditBus
from quant_trading.deploy import CanaryDeployer
from quant_trading.deployment import (
    CanaryController,
    CanaryMetrics,
    DeploymentDecision,
    ProductionReadinessError,
    assert_production_readiness,
    evaluate_production_readiness,
)


def _healthy() -> CanaryMetrics:
    return CanaryMetrics(error_rate=0.001, p99_latency_ms=25.0, sharpe_change_pct=-0.01)


def _complete_external_evidence() -> dict[str, str]:
    return {
        "external_worm_archive": "worm://archive/audit-chain/2026-05-29",
        "secret_manager": "vault://kv/quant/prod/accounts",
        "approval_service": "approval://risk-board/CONFIG-685/full-hash",
        "real_market_data_provider": "provider://tushare-pro/prod-feed/contract-2026",
        "provider_entitlement": "entitlement://vendor/a-share-pit/order-flow/contract-2026",
        "alt_archive_readiness": "artifact://alt-archive/v21/20260608/951be138",
        "exchange_position_provider": "exchange://broker-prod/reconciled-position-api",
        "broker_borrow_availability": "borrow-feed://broker-prod/a-share/shortable-feed",
        "paper_trading_90d": "paper://prod-sim/strategy-v18/2026-03-01_2026-05-31",
        "signed_plugin_review": "signature://plugins/strategy-pack/v1/reviewed",
        "production_calendar": "calendar://XSHG/vendor-approved/2026",
        "rollover_runbook": "runbook://futures-rollover/manual-approval/v1",
        "capacity_benchmark": "benchmark://prod-like/latency-capacity/2026-05",
        "failover_dr_test": "dr-test://multi-region/rto-rpo/2026-05",
    }


def test_canary_holds_until_full_window_then_advances_and_audits() -> None:
    audit = AuditBus()
    controller = CanaryController(audit_bus=audit)
    assert controller.percentage == 5
    assert controller.evaluate(_healthy(), observation_hours=23.9) == DeploymentDecision.HOLD
    assert controller.percentage == 5
    assert controller.evaluate(_healthy(), observation_hours=24) == DeploymentDecision.ADVANCE
    assert controller.percentage == 10
    assert audit.query("canary_decision")[-1]["payload"]["traffic_percentage"] == 10


@pytest.mark.parametrize(
    "metrics, reason",
    [
        (CanaryMetrics(0.051, 20.0, 0.0), "error_rate"),
        (CanaryMetrics(0.001, 201.0, 0.0), "p99_latency"),
        (CanaryMetrics(0.001, 20.0, -0.31), "sharpe_decline"),
    ],
)
def test_any_unhealthy_canary_metric_rolls_back(metrics: CanaryMetrics, reason: str) -> None:
    audit = AuditBus()
    controller = CanaryController(audit_bus=audit)
    assert controller.evaluate(metrics, observation_hours=2) == DeploymentDecision.ROLLBACK
    assert controller.percentage == 0
    assert reason in audit.query("canary_decision")[-1]["payload"]["reasons"]
    assert controller.evaluate(_healthy(), observation_hours=24) == DeploymentDecision.ROLLBACK


def test_canary_reaches_full_traffic_only_after_sequential_windows() -> None:
    controller = CanaryController()
    assert controller.evaluate(_healthy(), 24) == DeploymentDecision.ADVANCE
    assert controller.evaluate(_healthy(), 24) == DeploymentDecision.ADVANCE
    assert controller.evaluate(_healthy(), 24) == DeploymentDecision.COMPLETE
    assert controller.percentage == 100
    assert controller.evaluate(_healthy(), 24) == DeploymentDecision.COMPLETE


def test_legacy_deployer_cannot_take_arbitrary_unsafe_step() -> None:
    deployer = CanaryDeployer()
    with pytest.raises(ValueError, match="arbitrary"):
        deployer.increment(50)
    assert deployer.increment() == 10


def test_production_readiness_requires_all_external_evidence_and_audits() -> None:
    audit = AuditBus()
    report = evaluate_production_readiness(
        {
            "external_worm_archive": "worm://archive/audit-chain/2026-05-29",
            "secret_manager": "vault://kv/quant/prod/accounts",
        },
        audit_bus=audit,
    )
    assert report.passed is False
    assert any("approval_service" in blocker for blocker in report.blockers)
    assert any("provider_entitlement" in blocker for blocker in report.blockers)
    assert any("alt_archive_readiness" in blocker for blocker in report.blockers)
    assert any("exchange_position_provider" in blocker for blocker in report.blockers)
    assert any("broker_borrow_availability" in blocker for blocker in report.blockers)
    assert any("paper_trading_90d" in blocker for blocker in report.blockers)
    assert any("capacity_benchmark" in blocker for blocker in report.blockers)
    event = audit.query("production_readiness_evaluated")[-1]
    assert event["payload"]["passed"] is False
    assert "secret_manager" in event["payload"]["provided_keys"]


def test_production_readiness_rejects_local_or_placeholder_evidence() -> None:
    evidence = _complete_external_evidence()
    evidence["real_market_data_provider"] = "local-mock://pytest-provider"
    evidence["provider_entitlement"] = "contract://pending"
    evidence["alt_archive_readiness"] = "local://alt-archive/v21"
    evidence["broker_borrow_availability"] = "borrow-feed://sample"
    evidence["paper_trading_90d"] = "paper://todo"
    evidence["capacity_benchmark"] = "benchmark://pending"

    with pytest.raises(ProductionReadinessError) as exc:
        assert_production_readiness(evidence)

    message = str(exc.value)
    assert "real_market_data_provider" in message
    assert "placeholder/local evidence" in message
    assert "provider_entitlement" in message
    assert "alt_archive_readiness" in message
    assert "broker_borrow_availability" in message
    assert "paper_trading_90d" in message
    assert "capacity_benchmark" in message


def test_production_readiness_accepts_structurally_external_evidence_only() -> None:
    audit = AuditBus()
    report = assert_production_readiness(_complete_external_evidence(), audit_bus=audit)

    assert report.passed is True
    assert report.blockers == ()
    assert audit.query("production_readiness_evaluated")[-1]["payload"]["passed"] is True
