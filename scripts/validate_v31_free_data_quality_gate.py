#!/usr/bin/env python3
"""Validate V31 free-source research data quality.

The V31 gate is a research accelerator only. It can show that local free data
is usable for stronger diagnostics, but it never clears production blockers.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq
from _paths import PROJECT_DIR, RESULTS_DIR

DEFAULT_PIT_PATH = (
    PROJECT_DIR / "data" / "security_master" / "free_pit_approx" / "pit_universe_v31.parquet"
)
DEFAULT_STATUS_PATH = (
    PROJECT_DIR / "data" / "security_master" / "free_pit_approx" / "trading_status_v31.parquet"
)
DEFAULT_CORPORATE_ACTION_PATH = (
    PROJECT_DIR / "data" / "corporate_actions" / "free_reconciliation_v31.parquet"
)
DEFAULT_BUILD_REPORT_JSON = RESULTS_DIR / "quant_v31_free_data_build_report.json"
DEFAULT_OUTPUT_JSON = RESULTS_DIR / "quant_v31_free_data_quality_gate.json"
DEFAULT_OUTPUT_CSV = RESULTS_DIR / "quant_v31_free_data_quality_gate.csv"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pit-path", default=str(DEFAULT_PIT_PATH))
    parser.add_argument("--status-path", default=str(DEFAULT_STATUS_PATH))
    parser.add_argument("--corporate-action-path", default=str(DEFAULT_CORPORATE_ACTION_PATH))
    parser.add_argument("--build-report-json", default=str(DEFAULT_BUILD_REPORT_JSON))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument(
        "--min-entities",
        type=int,
        default=0,
        help="0 means auto-derive from V31 build-report universe size",
    )
    parser.add_argument("--min-dates", type=int, default=4500)
    parser.add_argument(
        "--min-corporate-action-entities",
        type=int,
        default=0,
        help="0 means auto-derive from V31 build-report universe size",
    )
    parser.add_argument(
        "--min-entity-coverage-ratio",
        type=float,
        default=0.95,
        help="Auto threshold ratio against symbols_requested/symbols_loaded when min counts are 0",
    )
    parser.add_argument("--require-free-research-ready", action="store_true")
    return parser.parse_args()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _read_schema_columns(path: Path) -> list[str]:
    return list(pq.read_schema(path).names)


def _column_stats(path: Path, *, date_column: str, entity_column: str) -> dict[str, Any]:
    table = pq.read_table(path, columns=[date_column, entity_column])
    frame = table.to_pandas()
    dates = pd.to_datetime(frame[date_column], errors="coerce").dropna()
    entities = frame[entity_column].astype(str).str.extract(r"(\d{6})", expand=False).dropna()
    return {
        "rows": int(table.num_rows),
        "unique_dates": int(dates.nunique()) if not dates.empty else 0,
        "unique_entities": int(entities.nunique()) if not entities.empty else 0,
        "date_start": dates.min().date().isoformat() if not dates.empty else None,
        "date_end": dates.max().date().isoformat() if not dates.empty else None,
    }


def _audit_file(
    *,
    requirement_id: str,
    title: str,
    path: Path,
    required_columns: tuple[str, ...],
    date_column: str,
    entity_column: str,
    min_dates: int,
    min_entities: int,
) -> dict[str, Any]:
    if not path.exists():
        return {
            "requirement_id": requirement_id,
            "title": title,
            "path": str(path),
            "exists": False,
            "rows": 0,
            "unique_dates": 0,
            "unique_entities": 0,
            "missing_columns": list(required_columns),
            "research_ready": False,
            "failures": ["missing_file"],
        }
    try:
        columns = _read_schema_columns(path)
        missing_columns = [column for column in required_columns if column not in columns]
        if missing_columns:
            stats = {"rows": 0, "unique_dates": 0, "unique_entities": 0}
        else:
            stats = _column_stats(path, date_column=date_column, entity_column=entity_column)
    except Exception as exc:
        return {
            "requirement_id": requirement_id,
            "title": title,
            "path": str(path),
            "exists": True,
            "rows": 0,
            "unique_dates": 0,
            "unique_entities": 0,
            "missing_columns": list(required_columns),
            "research_ready": False,
            "failures": [f"read_error:{type(exc).__name__}:{exc}"],
        }
    failures: list[str] = []
    if missing_columns:
        failures.append("missing_columns:" + ",".join(missing_columns))
    if int(stats["unique_dates"]) < min_dates:
        failures.append(f"unique_dates<{min_dates}")
    if int(stats["unique_entities"]) < min_entities:
        failures.append(f"unique_entities<{min_entities}")
    if int(stats["rows"]) <= 0:
        failures.append("empty_file")
    return {
        "requirement_id": requirement_id,
        "title": title,
        "path": str(path),
        "exists": True,
        "columns_present": columns,
        "missing_columns": missing_columns,
        "research_ready": not failures,
        "failures": failures,
        **stats,
    }


def _auto_min_entities(
    *,
    explicit_min: int,
    build_report: dict[str, Any],
    coverage_ratio: float,
) -> tuple[int, str]:
    """Resolve V31 research threshold against the actual requested universe.

    V31 originally targeted a 2000-name non-largecap research universe, so a
    fixed 1900 threshold was correct there. Lianghua's project-owned restored
    parquet set is currently a 1200-name universe; auto mode prevents a complete
    1200/1200 build from becoming a false research blocker while still failing a
    2000-name request that only loads ~1200 files.
    """

    if explicit_min > 0:
        return explicit_min, "explicit_cli"
    if not (0.0 < coverage_ratio <= 1.0):
        raise ValueError("min_entity_coverage_ratio must be within (0, 1]")
    summary = build_report.get("summary", {}) if isinstance(build_report, dict) else {}
    reference = summary.get("symbols_requested") or summary.get("symbols_loaded")
    if reference is None:
        raise ValueError(
            "cannot auto-derive V31 min_entities: build report lacks symbols_requested/symbols_loaded"
        )
    try:
        reference_count = int(reference)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "cannot auto-derive V31 min_entities: build report lacks symbols_requested/symbols_loaded"
        ) from exc
    return max(1, int(math.ceil(reference_count * coverage_ratio))), (
        f"auto_{coverage_ratio:.2%}_of_build_universe_{reference_count}"
    )


def build_report(
    *,
    pit_path: Path,
    status_path: Path,
    corporate_action_path: Path,
    build_report_path: Path,
    min_entities: int,
    min_dates: int,
    min_corporate_action_entities: int,
    generated_at: datetime,
    min_entity_coverage_ratio: float = 0.95,
) -> dict[str, Any]:
    build_report = _load_json(build_report_path)
    resolved_min_entities, min_entities_source = _auto_min_entities(
        explicit_min=min_entities,
        build_report=build_report,
        coverage_ratio=min_entity_coverage_ratio,
    )
    resolved_min_corporate_action_entities, min_corporate_action_entities_source = (
        _auto_min_entities(
            explicit_min=min_corporate_action_entities,
            build_report=build_report,
            coverage_ratio=min_entity_coverage_ratio,
        )
    )
    rows = [
        _audit_file(
            requirement_id="free_pit_security_master_approx",
            title="免费源 PIT 股票池近似",
            path=pit_path,
            required_columns=(
                "date",
                "code",
                "name",
                "list_date",
                "delist_date",
                "is_listed",
                "is_tradable",
                "is_large_cap",
                "source",
                "asof_date",
                "research_pit_approximation",
                "production_reconciled",
            ),
            date_column="date",
            entity_column="code",
            min_dates=min_dates,
            min_entities=resolved_min_entities,
        ),
        _audit_file(
            requirement_id="free_trading_status_approx",
            title="免费源交易状态近似",
            path=status_path,
            required_columns=(
                "date",
                "code",
                "is_st",
                "is_st_known",
                "is_suspended",
                "is_limit_up",
                "is_limit_down",
                "is_tradable",
                "price_limit_pct",
                "source",
                "asof_date",
                "research_pit_approximation",
                "production_reconciled",
            ),
            date_column="date",
            entity_column="code",
            min_dates=min_dates,
            min_entities=resolved_min_entities,
        ),
        _audit_file(
            requirement_id="free_corporate_action_column_audit",
            title="免费源复权/公司行动列审计",
            path=corporate_action_path,
            required_columns=(
                "date",
                "code",
                "action_type",
                "vendor_adjust_factor",
                "local_adjust_factor",
                "adjustment_diff_bps",
                "source",
                "evidence_ref",
                "has_adjustment_columns",
                "production_reconciled",
            ),
            date_column="date",
            entity_column="code",
            min_dates=1,
            min_entities=resolved_min_corporate_action_entities,
        ),
    ]
    research_blockers = [
        f"{item['requirement_id']}:{','.join(item['failures'])}"
        for item in rows
        if item.get("failures")
    ]
    free_research_ready = not research_blockers
    production_blockers = [
        "V31 是免费源/本地日线推断，不含 vendor PIT security master evidence ref。",
        "ST、退市、停牌、涨跌停和可成交状态仍含近似或未知字段，不能替代交易所/供应商历史状态表。",
        "公司行动只完成本地列审计，没有真实供应商复权因子 reconciliation。",
        "仍缺二十年真实 tick/minute、订单簿、券商订单/成交回放和 broker-backed borrow/position feed。",
        "因此 V31 不解除 V30 production-data gate 和外部 evidence gate blocker。",
    ]
    return {
        "ts": generated_at.isoformat(),
        "version": "V31-free-data-quality-gate",
        "research_only": True,
        "free_research_ready": free_research_ready,
        "production_data_ready": False,
        "thresholds": {
            "min_entities": resolved_min_entities,
            "min_dates": min_dates,
            "min_corporate_action_entities": resolved_min_corporate_action_entities,
            "min_entity_coverage_ratio": min_entity_coverage_ratio,
            "min_entities_source": min_entities_source,
            "min_corporate_action_entities_source": min_corporate_action_entities_source,
        },
        "requirements": rows,
        "summary": {
            "total_requirements": len(rows),
            "research_ready_count": sum(bool(item.get("research_ready")) for item in rows),
            "research_blocker_count": len(research_blockers),
            "production_blocker_count": len(production_blockers),
            "build_symbols_loaded": build_report.get("summary", {}).get("symbols_loaded"),
            "build_unique_trading_dates": build_report.get("summary", {}).get(
                "unique_trading_dates"
            ),
            "build_failure_count": build_report.get("failure_count"),
        },
        "build_report_path": str(build_report_path),
        "research_blockers": research_blockers,
        "production_blockers": production_blockers,
        "next_required_work": [
            "将 V31 输出接入研究回测的交易状态过滤，重新评估成交约束后的收益质量。",
            "为免费源每日快照增加 WORM 归档，逐日积累 future PIT 证据。",
            "如果要解除生产 blocker，仍需导入真实 vendor/broker 文件和外部 refs。",
        ],
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "requirement_id",
        "title",
        "exists",
        "rows",
        "unique_dates",
        "unique_entities",
        "research_ready",
        "failures",
        "path",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})


def write_report(report: dict[str, Any], *, output_json: Path, output_csv: Path) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(output_csv, report["requirements"])


def main() -> None:
    args = _parse_args()
    report = build_report(
        pit_path=Path(args.pit_path),
        status_path=Path(args.status_path),
        corporate_action_path=Path(args.corporate_action_path),
        build_report_path=Path(args.build_report_json),
        min_entities=args.min_entities,
        min_dates=args.min_dates,
        min_corporate_action_entities=args.min_corporate_action_entities,
        generated_at=datetime.now(timezone.utc),
        min_entity_coverage_ratio=args.min_entity_coverage_ratio,
    )
    write_report(report, output_json=Path(args.output_json), output_csv=Path(args.output_csv))
    print(
        "V31 free data quality gate: "
        f"free_research_ready={report['free_research_ready']} "
        f"production_data_ready={report['production_data_ready']} "
        f"research_ready={report['summary']['research_ready_count']}/"
        f"{report['summary']['total_requirements']} "
        f"research_blockers={report['summary']['research_blocker_count']}",
        flush=True,
    )
    print(f"Wrote {args.output_json}", flush=True)
    print(f"Wrote {args.output_csv}", flush=True)
    if args.require_free_research_ready and not report["free_research_ready"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
