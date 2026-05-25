import pytest
from quant_trading.core.audit import AuditBus
from quant_trading.portfolio.firewall import CapitalFirewall
from quant_trading.portfolio.lifecycle import LifecyclePolicy, Stage, StrategyLifecycle


def test_firewall_reallocation_does_not_double_count_and_reserves_pending_cash() -> None:
    audit = AuditBus()
    firewall = CapitalFirewall(100_000, max_per_strategy=0.5, audit_bus=audit)

    assert firewall.allocate("trend", 20_000)
    assert firewall.allocate("trend", 30_000)
    assert firewall.available() == 70_000
    assert firewall.reserve_pending("trend", 10_000)
    assert firewall.available() == 60_000
    assert not firewall.allocate("trend", 45_000)
    assert audit.verify_integrity()


def test_firewall_can_quarantine_risky_strategy() -> None:
    firewall = CapitalFirewall(100_000)
    assert firewall.allocate("bad_model", 20_000)

    assert firewall.isolate("bad_model", "risk limit exceeded") == 20_000
    assert not firewall.allocate("bad_model", 1)
    assert firewall.available() == 100_000


def test_live_transition_requires_approval_and_minimum_paper_duration() -> None:
    policy = LifecyclePolicy.from_yaml("config/portfolio.yaml")
    lifecycle = StrategyLifecycle("alpha", policy)

    lifecycle.request_transition(Stage.PAPER)
    assert lifecycle.approve_transition("risk_officer", "APP-1") == Stage.PAPER
    lifecycle.request_transition(Stage.SMALL_LIVE, {"days_in_current_stage": 89})
    with pytest.raises(PermissionError, match="duration"):
        lifecycle.approve_transition("risk_officer", "APP-2")
    lifecycle.request_transition(Stage.SMALL_LIVE, {"days_in_current_stage": 90})
    assert lifecycle.approve_transition("risk_officer", "APP-3") == Stage.SMALL_LIVE
    assert lifecycle.maximum_capital_pct == pytest.approx(0.01)


def test_direct_promotion_is_blocked_and_retirement_stops_signals() -> None:
    lifecycle = StrategyLifecycle("mean_reversion", LifecyclePolicy.from_yaml("config/portfolio.yaml"))
    with pytest.raises(PermissionError, match="direct promotion"):
        lifecycle.promote()
    lifecycle.request_transition(Stage.PAPER)
    lifecycle.approve_transition("risk", "APP-9")
    lifecycle.deprecate("risk", "APP-10")
    assert not lifecycle.accepting_signals
    lifecycle.archive("APP-11")
    assert lifecycle.stage == Stage.ARCHIVED
    assert lifecycle.audit_bus.verify_integrity()
