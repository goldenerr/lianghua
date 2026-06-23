#!/usr/bin/env python3
"""Run a local capacity and DR gate for the V40/V42 shadow path."""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _paths import PROJECT_DIR, RESULTS_DIR

sys.path.insert(0, str(PROJECT_DIR / "src"))

from quant_trading.core.audit import AuditBus
from quant_trading.core.events import Event, EventBus, EventType, OrderEvent, TickEvent
from quant_trading.operations_mr import FailoverManager, Region

DEFAULT_V42_GATE = RESULTS_DIR / "quant_v42_paper_shadow_evidence_gate.json"
DEFAULT_OUTPUT = RESULTS_DIR / "quant_v43_local_capacity_dr_gate.json"
DEFAULT_REPORT = Path("docs/research/quant_v43_local_capacity_dr_gate.md")


@dataclass(frozen=True)
class LocalCapacityThresholds:
    min_events_per_second: float = 10_000.0
    max_market_data_to_strategy_p99_ms: float = 10.0
    min_order_events_per_second: float = 500.0
    max_signal_to_gateway_p99_ms: float = 60.0
    max_failover_rto_seconds: float = 7200.0
    max_failover_rpo_seconds: float = 3600.0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v42-gate-json", default=str(DEFAULT_V42_GATE))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--report-md", default=str(DEFAULT_REPORT))
    parser.add_argument("--event-count", type=int, default=20_000)
    parser.add_argument("--order-count", type=int, default=2_000)
    parser.add_argument("--symbol-count", type=int, default=200)
    parser.add_argument("--strategy-count", type=int, default=20)
    parser.add_argument("--min-events-per-second", type=float, default=10_000.0)
    parser.add_argument("--min-order-events-per-second", type=float, default=500.0)
    parser.add_argument("--require-local-gate", action="store_true")
    return parser.parse_args()


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
    return ordered[index]


def _rss_mb() -> float:
    # macOS reports ru_maxrss in bytes, Linux in KB. The local project target is
    # macOS, but keep a conservative normalization fallback for portability.
    raw = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if raw > 10_000_000:
        return raw / 1024 / 1024
    return raw / 1024


def _load_v42_gate(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"paper_evidence_ready": False, "production_ready": False, "missing": True}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"V42 gate JSON must be an object: {path}")
    return data


def run_capacity_benchmark(
    *,
    event_count: int,
    order_count: int,
    symbol_count: int,
    strategy_count: int,
    audit: AuditBus,
) -> dict[str, Any]:
    if event_count <= 0 or order_count <= 0 or symbol_count <= 0 or strategy_count <= 0:
        raise ValueError("event/order/symbol/strategy counts must be positive")

    bus = EventBus()
    strategy_checksum = 0

    def strategy_handler(event: Event) -> None:
        nonlocal strategy_checksum
        price = float(event.payload.get("price", 0.0))
        # Simulate lightweight fan-out across strategy sleeves without creating
        # external side effects or order permissions.
        for strategy_id in range(strategy_count):
            strategy_checksum += int(price * 1000) ^ strategy_id

    bus.subscribe(EventType.TICK, strategy_handler)
    market_latencies_ms: list[float] = []
    start = time.perf_counter()
    for idx in range(event_count):
        event = TickEvent(
            f"LOCAL{idx % symbol_count:04d}",
            100.0 + (idx % 1000) * 0.0001,
            1.0 + (idx % 10),
        )
        before = time.perf_counter_ns()
        bus.publish(event)
        market_latencies_ms.append((time.perf_counter_ns() - before) / 1_000_000)
    elapsed = max(time.perf_counter() - start, 1e-9)

    gateway_latencies_ms: list[float] = []
    order_start = time.perf_counter()
    for idx in range(order_count):
        order = OrderEvent(
            f"LOCAL{idx % symbol_count:04d}",
            "BUY" if idx % 2 == 0 else "SELL",
            100,
            price=10.0 + (idx % 100) * 0.01,
            order_type="limit",
            live_order_submission_allowed=False,
        )
        before = time.perf_counter_ns()
        audit.record(
            "local_signal_to_gateway_benchmark",
            "v43_local_capacity_dr",
            {
                "symbol": order.symbol,
                "side": order.payload["side"],
                "quantity": order.payload["quantity"],
                "live_order_submission_allowed": False,
            },
        )
        gateway_latencies_ms.append((time.perf_counter_ns() - before) / 1_000_000)
    order_elapsed = max(time.perf_counter() - order_start, 1e-9)
    report = {
        "event_count": event_count,
        "order_count": order_count,
        "symbol_count": symbol_count,
        "strategy_count": strategy_count,
        "events_per_second": round(event_count / elapsed, 2),
        "order_events_per_second": round(order_count / order_elapsed, 2),
        "market_data_to_strategy_p50_ms": round(statistics.median(market_latencies_ms), 6),
        "market_data_to_strategy_p99_ms": round(_percentile(market_latencies_ms, 0.99), 6),
        "signal_to_gateway_p50_ms": round(statistics.median(gateway_latencies_ms), 6),
        "signal_to_gateway_p99_ms": round(_percentile(gateway_latencies_ms, 0.99), 6),
        "peak_rss_mb": round(_rss_mb(), 2),
        "audit_integrity_passes": audit.verify_integrity(),
        "strategy_checksum": strategy_checksum,
        "production_like": False,
    }
    audit.record("v43_local_capacity_benchmark_completed", "v43_local_capacity_dr", report)
    return report


def run_dr_drill(audit: AuditBus) -> dict[str, Any]:
    manager = FailoverManager()
    started = time.perf_counter()
    secondary_ok = manager.failover(Region.SECONDARY)
    rto_seconds = time.perf_counter() - started
    manager.set_replication_lag(0.5)
    status = manager.sync_status()
    manager.set_region_health(Region.TERTIARY, False)
    unhealthy_rejected = not manager.failover(Region.TERTIARY)
    report = {
        "secondary_failover_ok": bool(secondary_ok),
        "unhealthy_region_rejected": bool(unhealthy_rejected),
        "active_region": status["active"],
        "local_rto_seconds": round(rto_seconds, 6),
        "reported_rpo_seconds": float(status["lag_seconds"]),
        "production_like": False,
    }
    audit.record("v43_local_dr_drill_completed", "v43_local_capacity_dr", report)
    return report


def evaluate_local_gate(
    capacity: dict[str, Any],
    dr: dict[str, Any],
    thresholds: LocalCapacityThresholds,
) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    if float(capacity.get("events_per_second", 0.0)) < thresholds.min_events_per_second:
        blockers.append("本地事件吞吐低于门禁")
    if (
        float(capacity.get("market_data_to_strategy_p99_ms", float("inf")))
        > thresholds.max_market_data_to_strategy_p99_ms
    ):
        blockers.append("行情到策略 P99 延迟超过门禁")
    if float(capacity.get("order_events_per_second", 0.0)) < thresholds.min_order_events_per_second:
        blockers.append("本地订单事件吞吐低于门禁")
    if (
        float(capacity.get("signal_to_gateway_p99_ms", float("inf")))
        > thresholds.max_signal_to_gateway_p99_ms
    ):
        blockers.append("信号到网关 P99 延迟超过门禁")
    if not bool(capacity.get("audit_integrity_passes")):
        blockers.append("审计链完整性校验失败")
    if not bool(dr.get("secondary_failover_ok")):
        blockers.append("切换到备用区域失败")
    if not bool(dr.get("unhealthy_region_rejected")):
        blockers.append("不健康区域未被拒绝")
    if float(dr.get("local_rto_seconds", float("inf"))) > thresholds.max_failover_rto_seconds:
        blockers.append("本地 RTO 超过门禁")
    if float(dr.get("reported_rpo_seconds", float("inf"))) > thresholds.max_failover_rpo_seconds:
        blockers.append("本地 RPO 超过门禁")
    return not blockers, blockers


def build_payload(
    *,
    v42_gate: dict[str, Any],
    capacity: dict[str, Any],
    dr: dict[str, Any],
    thresholds: LocalCapacityThresholds,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or datetime.now(timezone.utc)
    local_passes, local_blockers = evaluate_local_gate(capacity, dr, thresholds)
    production_blockers = [
        "本地容量/DR 报告不是类生产环境或外部证明",
        "仍缺外部 capacity_benchmark 证据引用",
        "仍缺外部 failover_dr_test 证据引用",
    ]
    if not bool(v42_gate.get("paper_evidence_ready")):
        production_blockers.append("V42 影子模拟盘 90 天证据门禁未通过")
    return {
        "ts": generated_at.isoformat(),
        "version": "V43-local-capacity-dr-gate",
        "research_only": False,
        "production_like": False,
        "production_ready": False,
        "local_capacity_dr_gate_passes": local_passes,
        "v42_paper_evidence_ready": bool(v42_gate.get("paper_evidence_ready")),
        "thresholds": {
            "min_events_per_second": thresholds.min_events_per_second,
            "max_market_data_to_strategy_p99_ms": thresholds.max_market_data_to_strategy_p99_ms,
            "min_order_events_per_second": thresholds.min_order_events_per_second,
            "max_signal_to_gateway_p99_ms": thresholds.max_signal_to_gateway_p99_ms,
            "max_failover_rto_seconds": thresholds.max_failover_rto_seconds,
            "max_failover_rpo_seconds": thresholds.max_failover_rpo_seconds,
        },
        "capacity_benchmark_local": capacity,
        "failover_drill_local": dr,
        "local_blockers": local_blockers,
        "production_blockers": production_blockers,
        "note": "本地门禁用于工程回归，不能替代外部生产级容量压测和多区域容灾证明。",
    }


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    cap = payload["capacity_benchmark_local"]
    dr = payload["failover_drill_local"]
    lines = [
        "# V43 本地容量与容灾门禁",
        "",
        "状态：本地容量/DR 基准已生成，生产仍阻塞。",
        "",
        "## 结论",
        "",
        f"- `local_capacity_dr_gate_passes={str(payload['local_capacity_dr_gate_passes']).lower()}`",
        "- `production_like=false`",
        "- `production_ready=false`",
        f"- V42 影子模拟盘证据通过：{str(payload['v42_paper_evidence_ready']).lower()}",
        "",
        "## 本地容量指标",
        "",
        f"- 事件数：{cap['event_count']}",
        f"- 模拟品种数：{cap['symbol_count']}",
        f"- 模拟策略数：{cap['strategy_count']}",
        f"- 事件吞吐：{cap['events_per_second']} / 秒",
        f"- 行情到策略 P99：{cap['market_data_to_strategy_p99_ms']} ms",
        f"- 订单事件吞吐：{cap['order_events_per_second']} / 秒",
        f"- 信号到网关 P99：{cap['signal_to_gateway_p99_ms']} ms",
        f"- 峰值 RSS：{cap['peak_rss_mb']} MB",
        f"- 审计链完整性：{str(cap['audit_integrity_passes']).lower()}",
        "",
        "## 本地容灾指标",
        "",
        f"- 切换到备用区域：{str(dr['secondary_failover_ok']).lower()}",
        f"- 拒绝不健康区域：{str(dr['unhealthy_region_rejected']).lower()}",
        f"- 本地 RTO：{dr['local_rto_seconds']} 秒",
        f"- 报告 RPO：{dr['reported_rpo_seconds']} 秒",
        "",
        "## 本地阻塞项",
        "",
    ]
    if payload["local_blockers"]:
        lines.extend(f"- {item}" for item in payload["local_blockers"])
    else:
        lines.append("- 无")
    lines.extend(["", "## 生产阻塞项", ""])
    lines.extend(f"- {item}" for item in payload["production_blockers"])
    lines.extend(
        [
            "",
            "## 说明",
            "",
            "- 该报告只证明本地工程路径可跑通，不证明真实生产容量。",
            "- 生产仍必须提供独立的类生产压测、监控截图或报告引用，以及多区域容灾演练引用。",
            "- 本地结果不得填入生产 readiness gate 的 `capacity_benchmark` 或 `failover_dr_test`。",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    thresholds = LocalCapacityThresholds(
        min_events_per_second=args.min_events_per_second,
        min_order_events_per_second=args.min_order_events_per_second,
    )
    audit = AuditBus()
    v42_gate = _load_v42_gate(Path(args.v42_gate_json))
    capacity = run_capacity_benchmark(
        event_count=args.event_count,
        order_count=args.order_count,
        symbol_count=args.symbol_count,
        strategy_count=args.strategy_count,
        audit=audit,
    )
    dr = run_dr_drill(audit)
    payload = build_payload(v42_gate=v42_gate, capacity=capacity, dr=dr, thresholds=thresholds)
    output = Path(args.output_json)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(Path(args.report_md), payload)
    print(
        json.dumps(
            {
                "production_ready": payload["production_ready"],
                "production_like": payload["production_like"],
                "local_capacity_dr_gate_passes": payload["local_capacity_dr_gate_passes"],
                "events_per_second": capacity["events_per_second"],
                "market_data_to_strategy_p99_ms": capacity["market_data_to_strategy_p99_ms"],
                "signal_to_gateway_p99_ms": capacity["signal_to_gateway_p99_ms"],
                "output": str(output),
                "report": str(Path(args.report_md)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.require_local_gate and not payload["local_capacity_dr_gate_passes"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
