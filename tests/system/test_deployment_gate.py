import pytest
from quant_trading.core.audit import AuditBus
from quant_trading.deploy import CanaryDeployer
from quant_trading.deployment import CanaryController, CanaryMetrics, DeploymentDecision


def _healthy() -> CanaryMetrics:
    return CanaryMetrics(error_rate=0.001, p99_latency_ms=25.0, sharpe_change_pct=-0.01)


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
