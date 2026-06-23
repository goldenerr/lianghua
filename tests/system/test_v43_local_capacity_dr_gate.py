import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def _load_module():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "run_v43_local_capacity_dr_gate",
        SCRIPTS / "run_v43_local_capacity_dr_gate.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _capacity() -> dict:
    return {
        "event_count": 100,
        "order_count": 20,
        "symbol_count": 10,
        "strategy_count": 2,
        "events_per_second": 50_000.0,
        "order_events_per_second": 10_000.0,
        "market_data_to_strategy_p99_ms": 0.2,
        "signal_to_gateway_p99_ms": 0.5,
        "peak_rss_mb": 80.0,
        "audit_integrity_passes": True,
        "production_like": False,
    }


def _dr() -> dict:
    return {
        "secondary_failover_ok": True,
        "unhealthy_region_rejected": True,
        "active_region": "ap-southeast",
        "local_rto_seconds": 0.001,
        "reported_rpo_seconds": 0.5,
        "production_like": False,
    }


def test_local_gate_passes_locally_but_never_sets_production_ready() -> None:
    module = _load_module()
    payload = module.build_payload(
        v42_gate={"paper_evidence_ready": True},
        capacity=_capacity(),
        dr=_dr(),
        thresholds=module.LocalCapacityThresholds(),
        generated_at=datetime(2026, 6, 23, tzinfo=timezone.utc),
    )

    assert payload["local_capacity_dr_gate_passes"] is True
    assert payload["production_like"] is False
    assert payload["production_ready"] is False
    assert any("外部 capacity_benchmark" in item for item in payload["production_blockers"])
    assert any("外部 failover_dr_test" in item for item in payload["production_blockers"])


def test_v42_missing_paper_evidence_remains_a_production_blocker() -> None:
    module = _load_module()
    payload = module.build_payload(
        v42_gate={"paper_evidence_ready": False},
        capacity=_capacity(),
        dr=_dr(),
        thresholds=module.LocalCapacityThresholds(),
        generated_at=datetime(2026, 6, 23, tzinfo=timezone.utc),
    )

    assert payload["local_capacity_dr_gate_passes"] is True
    assert any("V42 影子模拟盘" in item for item in payload["production_blockers"])


def test_local_gate_rejects_latency_and_dr_failures() -> None:
    module = _load_module()
    capacity = _capacity()
    capacity["market_data_to_strategy_p99_ms"] = 11.0
    dr = _dr()
    dr["unhealthy_region_rejected"] = False

    passes, blockers = module.evaluate_local_gate(capacity, dr, module.LocalCapacityThresholds())

    assert passes is False
    assert "行情到策略 P99 延迟超过门禁" in blockers
    assert "不健康区域未被拒绝" in blockers


def test_real_small_local_benchmark_has_closed_live_order_flag() -> None:
    module = _load_module()
    audit = module.AuditBus()

    capacity = module.run_capacity_benchmark(
        event_count=50,
        order_count=10,
        symbol_count=5,
        strategy_count=2,
        audit=audit,
    )

    assert capacity["event_count"] == 50
    assert capacity["order_count"] == 10
    assert capacity["production_like"] is False
    assert capacity["audit_integrity_passes"] is True
    events = audit.query("local_signal_to_gateway_benchmark", limit=10)
    assert events
    assert all(event["payload"]["live_order_submission_allowed"] is False for event in events)
