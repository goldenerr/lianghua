#!/usr/bin/env python3
"""Validate V30 production-grade historical data and execution evidence."""

from __future__ import annotations

import argparse
import csv
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from _paths import PROJECT_DIR, RESULTS_DIR

DEFAULT_OUTPUT_JSON = RESULTS_DIR / "quant_v30_production_data_gate.json"
DEFAULT_OUTPUT_CSV = RESULTS_DIR / "quant_v30_production_data_gate.csv"
DEFAULT_20Y_AUDIT_JSON = RESULTS_DIR / "quant_v29_20y_data_coverage_audit.json"
DEFAULT_EXTERNAL_GATE_JSON = RESULTS_DIR / "quant_external_evidence_gate_v26.json"
DEFAULT_PIT_PANEL_JSON = RESULTS_DIR / "quant_pit_alpha_panel_readiness_v27_provider_qualified_2000.json"
DEFAULT_EVIDENCE_TEMPLATE = PROJECT_DIR / "config" / "production_data_evidence.v30.template.json"
PRICE_DIR = PROJECT_DIR / "data" / "parquet"
UNIVERSE = PROJECT_DIR / "data" / "stock_list_provider_qualified_non_largecap_2000_v27.json"


@dataclass(frozen=True)
class EvidenceRequirement:
    key: str
    description: str
    allowed_prefixes: tuple[str, ...]


@dataclass(frozen=True)
class DataRequirement:
    requirement_id: str
    title: str
    local_path: Path
    required_columns: tuple[str, ...]
    date_column: str | None
    entity_column: str | None
    min_unique_dates: int
    min_unique_entities: int
    evidence_key: str
    production_blocker: str


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-file", default=os.getenv("QUANT_V30_DATA_EVIDENCE_FILE", ""))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument("--audit-20y-json", default=str(DEFAULT_20Y_AUDIT_JSON))
    parser.add_argument("--external-gate-json", default=str(DEFAULT_EXTERNAL_GATE_JSON))
    parser.add_argument("--pit-panel-json", default=str(DEFAULT_PIT_PANEL_JSON))
    parser.add_argument("--require-production-data-ready", action="store_true")
    return parser.parse_args()


def _evidence_requirements() -> tuple[EvidenceRequirement, ...]:
    return (
        EvidenceRequirement(
            "pit_security_master",
            "历史时点证券主数据、上市/退市/可交易状态和非大盘 universe 生成证明",
            ("security-master://", "vendor-feed://", "entitlement://", "worm://", "s3-object-lock://"),
        ),
        EvidenceRequirement(
            "trading_status_actions",
            "退市、ST、停牌、涨跌停、可成交状态的交易日级历史证明",
            ("trading-status://", "exchange-feed://", "vendor-feed://", "entitlement://", "worm://"),
        ),
        EvidenceRequirement(
            "corporate_action_reconciliation",
            "真实供应商复权因子和 corporate action 复核证明",
            ("corporate-action://", "reconciliation://", "vendor-feed://", "artifact://", "worm://"),
        ),
        EvidenceRequirement(
            "real_tick_minute_execution_feed",
            "二十年真实 tick/minute 行情和订单簿/成交执行数据授权证明",
            ("tick-feed://", "minute-feed://", "exchange-feed://", "vendor-feed://", "entitlement://"),
        ),
        EvidenceRequirement(
            "exchange_order_fill_replay",
            "交易所或券商背书的历史订单/成交回放证明",
            ("broker-fill://", "exchange-fill://", "order-log://", "report://", "artifact://"),
        ),
    )


def _data_requirements() -> tuple[DataRequirement, ...]:
    base = PROJECT_DIR / "data"
    return (
        DataRequirement(
            "pit_security_master",
            "真正 PIT 历史股票池",
            base / "security_master" / "pit_universe.parquet",
            (
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
            ),
            "date",
            "code",
            4500,
            1900,
            "pit_security_master",
            "缺少可按交易日还原的 PIT security master，不能消除幸存者偏差。",
        ),
        DataRequirement(
            "trading_status_actions",
            "退市/ST/停牌/涨跌停可交易状态",
            base / "security_master" / "trading_status.parquet",
            (
                "date",
                "code",
                "is_st",
                "is_suspended",
                "is_limit_up",
                "is_limit_down",
                "is_tradable",
                "price_limit_pct",
                "source",
                "asof_date",
            ),
            "date",
            "code",
            4500,
            1900,
            "trading_status_actions",
            "缺少交易日级退市/ST/停牌/涨跌停状态，回测成交约束不可生产化。",
        ),
        DataRequirement(
            "corporate_action_reconciliation",
            "真实 vendor corporate action/复权复核",
            base / "corporate_actions" / "vendor_reconciliation.parquet",
            (
                "date",
                "code",
                "action_type",
                "vendor_adjust_factor",
                "local_adjust_factor",
                "adjustment_diff_bps",
                "source",
                "evidence_ref",
            ),
            "date",
            "code",
            500,
            1900,
            "corporate_action_reconciliation",
            "缺少供应商复权因子与本地价格序列复核，不能证明回测/实盘 corporate action 一致。",
        ),
        DataRequirement(
            "real_tick_minute_execution_feed",
            "二十年真实 tick/minute 执行历史",
            base / "execution_history" / "tick_minute_20y.parquet",
            (
                "timestamp",
                "code",
                "price",
                "volume",
                "bid",
                "ask",
                "trade_count",
                "source",
                "asof_date",
            ),
            "timestamp",
            "code",
            4500,
            1900,
            "real_tick_minute_execution_feed",
            "缺少真实 tick/minute 执行历史；合成 tick 只能用于研究，不能替代生产执行验证。",
        ),
        DataRequirement(
            "exchange_order_fill_replay",
            "交易所/券商订单成交回放",
            base / "execution_history" / "order_fill_replay.parquet",
            (
                "timestamp",
                "account_id",
                "client_order_id",
                "exchange_order_id",
                "code",
                "side",
                "order_qty",
                "fill_qty",
                "fill_price",
                "source",
                "evidence_ref",
            ),
            "timestamp",
            "code",
            250,
            100,
            "exchange_order_fill_replay",
            "缺少真实订单/成交回放，无法验证执行链路与交易所结果一致。",
        ),
    )


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _load_evidence(path_value: str) -> dict[str, str]:
    raw_env = os.getenv("QUANT_V30_DATA_EVIDENCE_JSON", "").strip()
    if raw_env:
        loaded = json.loads(raw_env)
    elif path_value:
        loaded = _load_json(Path(path_value))
    elif DEFAULT_EVIDENCE_TEMPLATE.exists():
        loaded = _load_json(DEFAULT_EVIDENCE_TEMPLATE)
    else:
        loaded = {}
    if not isinstance(loaded, dict):
        raise RuntimeError("V30 production data evidence must be a JSON object")
    evidence = {str(key): str(value) for key, value in loaded.items()}
    direct_env = {
        "pit_security_master": "QUANT_EVIDENCE_PIT_SECURITY_MASTER_REF",
        "trading_status_actions": "QUANT_EVIDENCE_TRADING_STATUS_ACTIONS_REF",
        "corporate_action_reconciliation": "QUANT_EVIDENCE_CORPORATE_ACTION_RECONCILIATION_REF",
        "real_tick_minute_execution_feed": "QUANT_EVIDENCE_REAL_TICK_MINUTE_EXECUTION_FEED_REF",
        "exchange_order_fill_replay": "QUANT_EVIDENCE_EXCHANGE_ORDER_FILL_REPLAY_REF",
    }
    for key, env_name in direct_env.items():
        value = os.getenv(env_name, "").strip()
        if value:
            evidence[key] = value
    return evidence


def _validate_evidence_ref(
    requirement: EvidenceRequirement,
    evidence: dict[str, str],
) -> tuple[bool, str | None]:
    reference = evidence.get(requirement.key, "").strip()
    if not reference:
        return False, f"{requirement.key}: 缺少 {requirement.description}"
    lowered = reference.lower()
    placeholders = ("todo", "pending", "tbd", "local", "mock", "dummy", "sample", "测试")
    if any(token in lowered for token in placeholders):
        return False, f"{requirement.key}: placeholder/local evidence 不可接受"
    if not reference.startswith(requirement.allowed_prefixes):
        allowed = ", ".join(requirement.allowed_prefixes)
        return False, f"{requirement.key}: evidence ref 必须使用 {allowed}"
    return True, None


def _date_values(frame: pd.DataFrame, column: str | None) -> pd.Series:
    if frame.empty or column is None:
        return pd.Series(dtype="datetime64[ns]")
    if column in frame.columns:
        return pd.to_datetime(frame[column], errors="coerce").dt.normalize()
    if column == "timestamp" and isinstance(frame.index, pd.DatetimeIndex):
        return pd.Series(frame.index, index=frame.index).dt.normalize()
    return pd.Series(dtype="datetime64[ns]")


def _entity_values(frame: pd.DataFrame, column: str | None) -> pd.Series:
    if frame.empty or column is None or column not in frame.columns:
        return pd.Series(dtype="object")
    values = frame[column].astype(str)
    if column == "code":
        values = values.str.extract(r"(\d{6})", expand=False).str.zfill(6)
    return values


def _audit_local_requirement(requirement: DataRequirement) -> dict[str, Any]:
    if not requirement.local_path.exists():
        return {
            "requirement_id": requirement.requirement_id,
            "title": requirement.title,
            "path": str(requirement.local_path),
            "exists": False,
            "rows": 0,
            "columns_present": [],
            "missing_columns": list(requirement.required_columns),
            "unique_dates": 0,
            "unique_entities": 0,
            "date_start": None,
            "date_end": None,
            "local_coverage_ready": False,
            "local_failures": ["missing_file"],
        }
    try:
        frame = pd.read_parquet(requirement.local_path)
    except Exception as exc:
        return {
            "requirement_id": requirement.requirement_id,
            "title": requirement.title,
            "path": str(requirement.local_path),
            "exists": True,
            "rows": 0,
            "columns_present": [],
            "missing_columns": list(requirement.required_columns),
            "unique_dates": 0,
            "unique_entities": 0,
            "date_start": None,
            "date_end": None,
            "local_coverage_ready": False,
            "local_failures": [f"read_error:{type(exc).__name__}:{exc}"],
        }
    columns_present = list(frame.columns)
    missing_columns = [column for column in requirement.required_columns if column not in frame.columns]
    dates = _date_values(frame, requirement.date_column).dropna()
    entities = _entity_values(frame, requirement.entity_column).dropna()
    unique_dates = int(dates.nunique()) if not dates.empty else 0
    unique_entities = int(entities.nunique()) if not entities.empty else 0
    failures: list[str] = []
    if frame.empty:
        failures.append("empty_file")
    if missing_columns:
        failures.append("missing_columns:" + ",".join(missing_columns))
    if unique_dates < requirement.min_unique_dates:
        failures.append(f"unique_dates<{requirement.min_unique_dates}")
    if unique_entities < requirement.min_unique_entities:
        failures.append(f"unique_entities<{requirement.min_unique_entities}")
    return {
        "requirement_id": requirement.requirement_id,
        "title": requirement.title,
        "path": str(requirement.local_path),
        "exists": True,
        "rows": int(len(frame)),
        "columns_present": columns_present,
        "missing_columns": missing_columns,
        "unique_dates": unique_dates,
        "unique_entities": unique_entities,
        "date_start": dates.min().date().isoformat() if not dates.empty else None,
        "date_end": dates.max().date().isoformat() if not dates.empty else None,
        "local_coverage_ready": not failures,
        "local_failures": failures,
    }


def _audit_price_adjustment_columns(max_files: int = 200) -> dict[str, Any]:
    raw_codes = []
    if UNIVERSE.exists():
        loaded = json.loads(UNIVERSE.read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            raw_codes = [str(item).zfill(6) for item in loaded if isinstance(item, str)]
    files = [PRICE_DIR / f"{code}.parquet" for code in raw_codes[:max_files]]
    existing = [path for path in files if path.exists()]
    adjustment_columns = {
        "adj_factor",
        "adjust_factor",
        "qfq_factor",
        "hfq_factor",
        "adjusted_close",
        "close_qfq",
        "close_hfq",
    }
    files_with_adjustment = 0
    sampled_columns: dict[str, list[str]] = {}
    for path in existing:
        try:
            columns = list(pd.read_parquet(path, columns=None).columns)
        except Exception:
            columns = []
        sampled_columns[path.stem] = columns
        if adjustment_columns.intersection(columns):
            files_with_adjustment += 1
    return {
        "universe_path": str(UNIVERSE),
        "price_dir": str(PRICE_DIR),
        "sampled_files": len(existing),
        "files_with_adjustment_columns": files_with_adjustment,
        "adjustment_column_candidates": sorted(adjustment_columns),
        "sampled_columns_head": dict(list(sampled_columns.items())[:10]),
        "local_adjustment_columns_ready": bool(existing) and files_with_adjustment == len(existing),
    }


def _summarize_existing_intraday() -> dict[str, Any]:
    path = PROJECT_DIR / "data" / "intraday" / "microstructure_features.parquet"
    if not path.exists():
        return {"path": str(path), "exists": False}
    frame = pd.read_parquet(path)
    dates = pd.to_datetime(frame.get("date"), errors="coerce").dropna()
    codes = frame.get("code", pd.Series(dtype="object")).astype(str).str.extract(r"(\d{6})", expand=False)
    return {
        "path": str(path),
        "exists": True,
        "rows": int(len(frame)),
        "unique_dates": int(dates.nunique()) if not dates.empty else 0,
        "unique_entities": int(codes.dropna().nunique()) if not codes.empty else 0,
        "date_start": dates.min().date().isoformat() if not dates.empty else None,
        "date_end": dates.max().date().isoformat() if not dates.empty else None,
        "note": "这是近期分钟特征诊断，不是二十年真实 tick/minute 执行历史。",
    }


def _build_requirement_rows(evidence: dict[str, str]) -> list[dict[str, Any]]:
    evidence_specs = {item.key: item for item in _evidence_requirements()}
    rows: list[dict[str, Any]] = []
    for requirement in _data_requirements():
        local = _audit_local_requirement(requirement)
        evidence_ok, evidence_failure = _validate_evidence_ref(
            evidence_specs[requirement.evidence_key],
            evidence,
        )
        local_ready = bool(local["local_coverage_ready"])
        ready = local_ready and evidence_ok
        rows.append(
            {
                **local,
                "evidence_key": requirement.evidence_key,
                "evidence_ref_provided": bool(evidence.get(requirement.evidence_key, "").strip()),
                "evidence_ready": evidence_ok,
                "evidence_failure": evidence_failure,
                "production_ready": ready,
                "production_blocker": None if ready else requirement.production_blocker,
            }
        )
    return rows


def _external_summaries(
    *,
    audit_20y_path: Path,
    external_gate_path: Path,
    pit_panel_path: Path,
) -> dict[str, Any]:
    audit_20y = _load_json(audit_20y_path)
    external_gate = _load_json(external_gate_path)
    pit_panel = _load_json(pit_panel_path)
    assessment = audit_20y.get("assessment", {}) if isinstance(audit_20y, dict) else {}
    pit_summary = pit_panel.get("summary", {}) if isinstance(pit_panel, dict) else {}
    return {
        "twenty_year_audit": {
            "path": str(audit_20y_path),
            "exists": audit_20y_path.exists(),
            "can_claim_20y_full_universe_production_validation": bool(
                assessment.get("can_claim_20y_full_universe_production_validation")
            ),
            "can_run_20y_diagnostic_backtest": bool(assessment.get("can_run_20y_diagnostic_backtest")),
            "diagnostic_start_after_warmup": assessment.get("diagnostic_start_after_warmup"),
            "production_full_universe_start_after_warmup": assessment.get(
                "production_full_universe_start_after_warmup"
            ),
            "minimum_production_symbols": assessment.get("minimum_production_symbols"),
        },
        "external_production_evidence_gate": {
            "path": str(external_gate_path),
            "exists": external_gate_path.exists(),
            "production_ready": bool(external_gate.get("production_ready")),
            "blocker_count": len(external_gate.get("missing_or_untrusted_blockers", []))
            if isinstance(external_gate.get("missing_or_untrusted_blockers"), list)
            else None,
        },
        "pit_alpha_panel_gate": {
            "path": str(pit_panel_path),
            "exists": pit_panel_path.exists(),
            "production_data_ready": bool(pit_panel.get("production_data_ready")),
            "research_ready_panels": pit_summary.get("research_ready_panels"),
            "total_panels": pit_summary.get("total_panels"),
        },
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "requirement_id",
        "title",
        "exists",
        "rows",
        "unique_dates",
        "unique_entities",
        "local_coverage_ready",
        "local_failures",
        "evidence_key",
        "evidence_ref_provided",
        "evidence_ready",
        "evidence_failure",
        "production_ready",
        "production_blocker",
        "path",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})


def build_report(
    *,
    evidence: dict[str, str],
    audit_20y_path: Path,
    external_gate_path: Path,
    pit_panel_path: Path,
    generated_at: datetime,
) -> dict[str, Any]:
    rows = _build_requirement_rows(evidence)
    external = _external_summaries(
        audit_20y_path=audit_20y_path,
        external_gate_path=external_gate_path,
        pit_panel_path=pit_panel_path,
    )
    requirement_blockers = [
        item["production_blocker"]
        for item in rows
        if item["production_blocker"]
    ]
    evidence_failures = [
        item["evidence_failure"]
        for item in rows
        if item["evidence_failure"]
    ]
    coverage_failures = [
        f"{item['requirement_id']}:{','.join(item['local_failures'])}"
        for item in rows
        if item["local_failures"]
    ]
    cross_gate_blockers: list[str] = []
    if not external["twenty_year_audit"]["can_claim_20y_full_universe_production_validation"]:
        cross_gate_blockers.append("二十年覆盖审计未达到全量生产验证口径。")
    if not external["external_production_evidence_gate"]["production_ready"]:
        cross_gate_blockers.append("外部生产 evidence gate 未通过。")
    if not external["pit_alpha_panel_gate"]["production_data_ready"]:
        cross_gate_blockers.append("PIT alpha panel gate 未达到生产数据就绪。")

    production_data_ready = not requirement_blockers and not evidence_failures and not cross_gate_blockers
    return {
        "ts": generated_at.isoformat(),
        "version": "V30-production-data-gate",
        "research_only": True,
        "production_data_ready": production_data_ready,
        "requirements": rows,
        "summary": {
            "total_requirements": len(rows),
            "local_coverage_ready_count": sum(bool(item["local_coverage_ready"]) for item in rows),
            "evidence_ready_count": sum(bool(item["evidence_ready"]) for item in rows),
            "production_ready_count": sum(bool(item["production_ready"]) for item in rows),
            "requirement_blocker_count": len(requirement_blockers),
            "evidence_failure_count": len(evidence_failures),
            "cross_gate_blocker_count": len(cross_gate_blockers),
        },
        "external_gate_inputs": external,
        "diagnostics": {
            "existing_intraday_microstructure": _summarize_existing_intraday(),
            "daily_price_adjustment_columns": _audit_price_adjustment_columns(),
        },
        "blockers": [
            *requirement_blockers,
            *coverage_failures,
            *evidence_failures,
            *cross_gate_blockers,
        ],
        "next_required_work": [
            "接入供应商 PIT security master，并每日 WORM 归档。",
            "接入退市、ST、停牌、涨跌停和可成交状态历史表。",
            "接入 vendor corporate action/复权因子复核表，并与本地价格序列对账。",
            "采购或接入二十年真实 tick/minute 行情与订单簿/成交执行数据。",
            "导入券商/交易所订单成交日志，完成历史订单/成交回放验证。",
            "补齐外部 WORM、Secret Manager、Approval、Provider Entitlement、Broker Position、90 天 Paper、容量与 DR evidence refs。",
        ],
    }


def write_report(report: dict[str, Any], *, output_json: Path, output_csv: Path) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(output_csv, report["requirements"])


def main() -> None:
    args = _parse_args()
    evidence = _load_evidence(args.evidence_file)
    report = build_report(
        evidence=evidence,
        audit_20y_path=Path(args.audit_20y_json),
        external_gate_path=Path(args.external_gate_json),
        pit_panel_path=Path(args.pit_panel_json),
        generated_at=datetime.now(timezone.utc),
    )
    write_report(report, output_json=Path(args.output_json), output_csv=Path(args.output_csv))
    print(
        "V30 production data gate: "
        f"production_data_ready={report['production_data_ready']} "
        f"requirements={report['summary']['production_ready_count']}/{report['summary']['total_requirements']} "
        f"blockers={len(report['blockers'])}",
        flush=True,
    )
    print(f"Wrote {args.output_json}", flush=True)
    print(f"Wrote {args.output_csv}", flush=True)
    if args.require_production_data_ready and not report["production_data_ready"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
