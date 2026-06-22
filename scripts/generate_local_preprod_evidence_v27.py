#!/usr/bin/env python3
"""Generate local pre-production evidence for V27.

The reports created here are useful for engineering readiness, but they are not
external production evidence. They must not be fed into the production release
gate as substitutes for WORM/provider/approval/broker attestations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from _paths import DATA_DIR, PROJECT_DIR, RESULTS_DIR

sys.path.insert(0, str(PROJECT_DIR / "src"))

from quant_trading.core.audit import AuditBus, InMemorySecretManager, LocalAuditWormArchive
from quant_trading.core.events import EventBus, TickEvent
from quant_trading.operations_mr import FailoverManager, Region
from quant_trading.paper_trading import V59_CONFIG, PaperTradingEngine

DEFAULT_UNIVERSE = PROJECT_DIR / "data" / "stock_list_provider_qualified_non_largecap_2000_v27.json"
DEFAULT_OUTPUT_JSON = RESULTS_DIR / "quant_v27_local_preprod_evidence.json"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--universe",
        default=os.getenv("QUANT_V27_LOCAL_EVIDENCE_UNIVERSE", str(DEFAULT_UNIVERSE)),
        help="Target universe JSON for paper dry-run coverage metadata.",
    )
    parser.add_argument(
        "--paper-days",
        type=int,
        default=int(os.getenv("QUANT_V27_LOCAL_EVIDENCE_PAPER_DAYS", "90")),
        help="Historical trading-day count for local paper dry-run.",
    )
    parser.add_argument(
        "--event-count",
        type=int,
        default=int(os.getenv("QUANT_V27_LOCAL_EVIDENCE_EVENT_COUNT", "50000")),
        help="Event-bus messages for local capacity benchmark.",
    )
    parser.add_argument(
        "--output-json",
        default=os.getenv("QUANT_V27_LOCAL_EVIDENCE_OUTPUT_JSON", str(DEFAULT_OUTPUT_JSON)),
        help="Output JSON path.",
    )
    return parser.parse_args()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_codes(path: Path) -> list[str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise RuntimeError(f"universe must be a JSON string array: {path}")
    return [str(item).zfill(6) for item in raw]


def _load_price_frames(codes: list[str]) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for code in codes:
        path = DATA_DIR / f"{code}.parquet"
        if not path.exists():
            continue
        frame = pd.read_parquet(path)
        required = {"close", "volume"}
        if required.issubset(frame.columns) and len(frame) >= 252:
            frames[code] = frame.sort_index()
    return frames


def _capacity_benchmark(event_count: int, audit: AuditBus) -> dict[str, Any]:
    bus = EventBus()
    started = time.perf_counter()
    for idx in range(event_count):
        bus.publish(TickEvent("LOCAL-CAPACITY", 100.0 + idx * 0.0001, 1.0))
    elapsed = time.perf_counter() - started
    rate = event_count / max(elapsed, 1e-9)
    report = {
        "event_count": event_count,
        "elapsed_s": round(elapsed, 6),
        "events_per_second": round(rate, 2),
        "target_events_per_second_local_min": 10000,
        "passes_local_minimum": rate >= 10000,
        "production_like": False,
    }
    audit.record("local_capacity_benchmark_completed", "local_preprod_evidence", report)
    return report


def _failover_drill(audit: AuditBus) -> dict[str, Any]:
    manager = FailoverManager()
    started = time.perf_counter()
    ok_secondary = manager.failover(Region.SECONDARY)
    rto_ms = (time.perf_counter() - started) * 1000
    manager.set_replication_lag(0.5)
    status = manager.sync_status()
    manager.set_region_health(Region.TERTIARY, False)
    unhealthy_rejected = not manager.failover(Region.TERTIARY)
    report = {
        "secondary_failover_ok": ok_secondary,
        "unhealthy_region_rejected": unhealthy_rejected,
        "active_region": status["active"],
        "local_rto_ms": round(rto_ms, 4),
        "reported_replication_lag_seconds": status["lag_seconds"],
        "rto_target_seconds": 7200,
        "rpo_target_seconds": 3600,
        "passes_local_drill": bool(ok_secondary and unhealthy_rejected and status["lag_seconds"] <= 1.0),
        "production_like": False,
    }
    audit.record("local_failover_drill_completed", "local_preprod_evidence", report)
    return report


def _build_snapshot(
    frames: dict[str, pd.DataFrame],
    idx_by_symbol: dict[str, int],
    current_date: pd.Timestamp,
) -> tuple[dict[str, dict[str, np.ndarray]], dict[str, float]]:
    snapshot: dict[str, dict[str, np.ndarray]] = {}
    prices: dict[str, float] = {}
    for code, frame in frames.items():
        idx = idx_by_symbol[code]
        if idx < 252 or idx >= len(frame):
            continue
        window = frame.iloc[idx - 251 : idx + 1]
        if len(window) < 252:
            continue
        close = window["close"].to_numpy(dtype=float)
        volume = window["volume"].to_numpy(dtype=float)
        price = float(frame.iloc[idx]["close"])
        if not np.isfinite(price) or price <= 0:
            continue
        snapshot[code] = {"close": close, "volume": volume}
        prices[code] = price
    return snapshot, prices


def _paper_dry_run(
    codes: list[str],
    *,
    paper_days: int,
    audit: AuditBus,
) -> dict[str, Any]:
    frames = _load_price_frames(codes)
    if not frames:
        raise RuntimeError("no price frames available for local paper dry-run")
    idx_by_date = {
        code: {pd.Timestamp(index): idx for idx, index in enumerate(frame.index)}
        for code, frame in frames.items()
    }
    all_dates = sorted(pd.Timestamp(item) for item in set().union(*(set(mapping) for mapping in idx_by_date.values())))
    warm_counts_by_date: dict[pd.Timestamp, int] = {}
    for current_date in all_dates:
        warm_counts_by_date[current_date] = sum(
            1
            for mapping in idx_by_date.values()
            if (idx := mapping.get(current_date)) is not None and idx >= 252
        )
    requested_min_symbol_coverage = min(len(frames), max(50, int(len(codes) * 0.80)))
    coverage_candidates = sorted(
        {
            requested_min_symbol_coverage,
            *(max(50, int(len(frames) * ratio)) for ratio in (0.95, 0.90, 0.80, 0.70, 0.60, 0.50)),
        },
        reverse=True,
    )
    min_symbol_coverage = 0
    eligible_dates: list[pd.Timestamp] = []
    for candidate in coverage_candidates:
        candidate_dates = [
            current_date
            for current_date, warm_symbol_count in warm_counts_by_date.items()
            if warm_symbol_count >= candidate
        ]
        if len(candidate_dates) >= paper_days:
            min_symbol_coverage = candidate
            eligible_dates = candidate_dates
            break
    if len(eligible_dates) < paper_days:
        raise RuntimeError(
            "not enough high-coverage dates for paper dry-run: "
            f"have {len(eligible_dates)}, need {paper_days}, candidates={coverage_candidates}"
        )
    selected_dates = eligible_dates[-paper_days:]
    research_price_frame_target = int(len(codes) * 0.95)
    data_coverage_blockers: list[str] = []
    if len(frames) < research_price_frame_target:
        data_coverage_blockers.append(
            f"daily price frames cover {len(frames)}/{len(codes)} target symbols; "
            f"research target is >= {research_price_frame_target}"
        )
    if min_symbol_coverage < requested_min_symbol_coverage:
        data_coverage_blockers.append(
            f"paper dry-run relaxed min daily warm-symbol coverage from "
            f"{requested_min_symbol_coverage} to {min_symbol_coverage} to obtain {paper_days} dates"
        )
    engine = PaperTradingEngine(
        initial_capital=1_000_000,
        config={**V59_CONFIG, "rebalance_freq": 20, "top_n": 35, "max_per_sector": 5},
        industry_data_path=PROJECT_DIR / "data" / "industry_fixed.parquet",
    )
    rebalance_days = 0
    price_coverage: list[int] = []
    for day_idx, current_date in enumerate(selected_dates):
        tradable = {
            code: frame
            for code, frame in frames.items()
            if current_date in idx_by_date[code]
        }
        idx_by_symbol = {code: idx_by_date[code][current_date] for code in tradable}
        snapshot, prices = _build_snapshot(tradable, idx_by_symbol, current_date)
        price_coverage.append(len(prices))
        if day_idx == 0 or day_idx % int(engine.config["rebalance_freq"]) == 0:
            targets = engine.compute_positions(snapshot, current_date)
            engine.execute_rebalance(targets, prices, current_date.date().isoformat())
            rebalance_days += 1
        engine.account.update_market_values(prices)
        engine.days_traded += 1
        if engine.stopped:
            break
    report = engine.generate_report()
    report.update(
        {
            "paper_window_start": selected_dates[0].date().isoformat(),
            "paper_window_end": selected_dates[-1].date().isoformat(),
            "requested_days": paper_days,
            "completed_days": engine.days_traded,
            "rebalance_days": rebalance_days,
            "target_universe_size": len(codes),
            "price_frame_symbols": len(frames),
            "eligible_high_coverage_dates": len(eligible_dates),
            "requested_min_symbol_coverage": requested_min_symbol_coverage,
            "min_symbol_coverage_required": min_symbol_coverage,
            "avg_price_coverage": round(float(np.mean(price_coverage)), 2) if price_coverage else 0.0,
            "min_price_coverage": int(min(price_coverage)) if price_coverage else 0,
            "research_price_frame_target": research_price_frame_target,
            "price_frame_coverage_passes_research": len(frames) >= research_price_frame_target,
            "data_coverage_blockers": data_coverage_blockers,
            "production_like": False,
            "production_blocker": "local historical dry-run is not a real 90-day paper-trading service",
        }
    )
    audit.record("local_paper_trading_dry_run_completed", "local_preprod_evidence", report)
    return report


def _local_audit_archive(audit: AuditBus) -> dict[str, Any]:
    archive = LocalAuditWormArchive(
        PROJECT_DIR / "data" / "local_preprod_audit_archive",
        secret_provider=InMemorySecretManager({"test://local-preprod-audit-key": "local-preprod-only"}),
        key_ref="test://local-preprod-audit-key",
    )
    target_date = datetime.now(timezone.utc).date()
    day_root = PROJECT_DIR / "data" / "local_preprod_audit_archive" / target_date.isoformat()
    if day_root.exists():
        suffix = 1
        while (PROJECT_DIR / "data" / f"local_preprod_audit_archive_{suffix}" / target_date.isoformat()).exists():
            suffix += 1
        archive = LocalAuditWormArchive(
            PROJECT_DIR / "data" / f"local_preprod_audit_archive_{suffix}",
            secret_provider=InMemorySecretManager({"test://local-preprod-audit-key": "local-preprod-only"}),
            key_ref="test://local-preprod-audit-key",
        )
    result = archive.archive_day(audit, target_date)
    verified = archive.verify_day(target_date)
    return {
        "archive_date": result.archive_date.isoformat(),
        "event_count": result.event_count,
        "log_path": str(result.log_path),
        "manifest_path": str(result.manifest_path),
        "log_sha256": result.log_sha256,
        "manifest_hash": result.manifest_hash,
        "attestation_ref": result.attestation_ref,
        "verified": verified,
        "production_like": False,
    }


def main() -> None:
    args = _parse_args()
    started = time.perf_counter()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = Path(args.output_json)
    if not output_path.is_absolute():
        output_path = RESULTS_DIR / output_path
    universe_path = Path(args.universe)
    codes = _read_codes(universe_path)
    audit = AuditBus()
    audit.record(
        "local_preprod_evidence_started",
        "local_preprod_evidence",
        {"universe": str(universe_path), "paper_days": args.paper_days},
    )
    capacity = _capacity_benchmark(args.event_count, audit)
    failover = _failover_drill(audit)
    paper = _paper_dry_run(codes, paper_days=args.paper_days, audit=audit)
    archive = _local_audit_archive(audit)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V27-local-preprod-evidence",
        "research_only": True,
        "production_ready": False,
        "universe": {
            "path": str(universe_path),
            "size": len(codes),
            "sha256": _sha256_file(universe_path),
        },
        "capacity_benchmark_local": capacity,
        "failover_drill_local": failover,
        "paper_trading_dry_run_local": paper,
        "local_audit_archive": archive,
        "production_blockers": [
            "local reports are not accepted as external production evidence",
            "real WORM, Secret Manager, Approval Service, provider entitlement, broker position and borrow feeds remain required",
            "paper_trading_90d requires a continuously running approved paper service for at least 90 calendar/trading days",
        ],
        "elapsed_s": round(time.perf_counter() - started, 3),
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {output_path}", flush=True)
    print(
        "Local preprod evidence: "
        f"capacity={capacity['events_per_second']}/s "
        f"failover_ok={failover['passes_local_drill']} "
        f"paper_days={paper['completed_days']} "
        f"paper_sharpe={paper.get('sharpe_ratio')}",
        flush=True,
    )


if __name__ == "__main__":
    main()
