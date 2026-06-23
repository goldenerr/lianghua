#!/usr/bin/env python3
"""Ingest paper-shadow daily reports and refresh V42/V44 gates."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

from _paths import PROJECT_DIR, RESULTS_DIR

DEFAULT_LEDGER = RESULTS_DIR / "quant_v45_paper_shadow_ledger.json"
DEFAULT_SUMMARY = RESULTS_DIR / "quant_v45_paper_shadow_ingestion.json"
DEFAULT_REPORT = Path("docs/research/quant_v45_paper_shadow_ingestion.md")
DEFAULT_V40_PACKAGE = RESULTS_DIR / "quant_v40_paper_shadow_package.json"
DEFAULT_V42_OUTPUT = RESULTS_DIR / "quant_v42_paper_shadow_evidence_gate.json"
DEFAULT_V42_REPORT = Path("docs/research/quant_v42_paper_shadow_evidence_gate.md")
DEFAULT_V43_GATE = RESULTS_DIR / "quant_v43_local_capacity_dr_gate.json"
DEFAULT_V44_OUTPUT = RESULTS_DIR / "quant_v44_production_readiness_gate.json"
DEFAULT_V44_REPORT = Path("docs/research/quant_v44_production_readiness_gate.md")

SENSITIVE_KEY_RE = re.compile(r"(^|[_-])(token|password|api[_-]?key|private[_-]?key)([_-]|$)", re.I)
SENSITIVE_VALUE_RE = re.compile(r"(Bearer\s+[A-Za-z0-9._=-]+|sk-[A-Za-z0-9]{12,}|AKIA[0-9A-Z]{16})")
TRUSTED_ARCHIVE_PREFIXES = ("worm://", "archive://", "s3://worm-")
TRUSTED_PROVIDER_PREFIXES = ("provider://", "vendor://")
TRUSTED_BROKER_PREFIXES = ("broker://", "exchange://")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--daily-report-json",
        default="",
        help="Optional single-day JSON report to append to the V45 ledger.",
    )
    parser.add_argument("--allow-replace", action="store_true")
    parser.add_argument("--ledger-json", default=str(DEFAULT_LEDGER))
    parser.add_argument("--summary-json", default=str(DEFAULT_SUMMARY))
    parser.add_argument("--report-md", default=str(DEFAULT_REPORT))
    parser.add_argument("--v40-package-json", default=str(DEFAULT_V40_PACKAGE))
    parser.add_argument("--v42-output-json", default=str(DEFAULT_V42_OUTPUT))
    parser.add_argument("--v42-report-md", default=str(DEFAULT_V42_REPORT))
    parser.add_argument("--v43-gate-json", default=str(DEFAULT_V43_GATE))
    parser.add_argument("--v44-output-json", default=str(DEFAULT_V44_OUTPUT))
    parser.add_argument("--v44-report-md", default=str(DEFAULT_V44_REPORT))
    parser.add_argument(
        "--evidence-file",
        default=os.getenv("QUANT_PRODUCTION_EVIDENCE_FILE", ""),
        help="Optional external production evidence refs JSON for V44 refresh.",
    )
    parser.add_argument("--require-paper-evidence-ready", action="store_true")
    return parser.parse_args()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"JSON must be an object: {path}")
    return data


def _load_script_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        raise RuntimeError(f"cannot load module: {path}")
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _scan_sensitive_material(node: Any, path: str = "$") -> list[str]:
    blockers: list[str] = []
    if isinstance(node, dict):
        for raw_key, value in node.items():
            key = str(raw_key)
            child_path = f"{path}.{key}"
            if SENSITIVE_KEY_RE.search(key):
                blockers.append(f"发现疑似敏感字段，禁止写入日报：{child_path}")
            blockers.extend(_scan_sensitive_material(value, child_path))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            blockers.extend(_scan_sensitive_material(value, f"{path}[{index}]"))
    elif isinstance(node, str) and SENSITIVE_VALUE_RE.search(node):
        blockers.append(f"发现疑似敏感值，禁止写入日报：{path}")
    return blockers


def _as_float(raw: Any, field: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是数字") from exc
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{field} 必须是有限数字")
    return value


def _as_int(raw: Any, field: str) -> int:
    value = _as_float(raw, field)
    if abs(value - round(value)) > 1e-9:
        raise ValueError(f"{field} 必须是整数")
    return int(round(value))


def _as_bool(raw: Any, field: str) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    raise ValueError(f"{field} 必须是布尔值")


def _as_date(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("date 必须是 YYYY-MM-DD 字符串")
    return date.fromisoformat(raw.strip()).isoformat()


def _string_field(row: dict[str, Any], field: str) -> str:
    value = str(row.get(field, "")).strip()
    if not value:
        raise ValueError(f"{field} 不能为空")
    return value


def _raw_daily_body(raw: dict[str, Any]) -> dict[str, Any]:
    body = raw.get("daily_report", raw)
    if not isinstance(body, dict):
        raise ValueError("daily_report 必须是 JSON 对象")
    return dict(body)


def _audit_hash_from_raw(body: dict[str, Any]) -> tuple[str, int]:
    if body.get("audit_hash"):
        return str(body["audit_hash"]).strip().lower(), _as_int(body.get("audit_event_count"), "audit_event_count")
    if isinstance(body.get("audit_events"), list):
        lines = [_canonical_json(event).encode("utf-8") for event in body["audit_events"]]
        payload = b"\n".join(lines) + (b"\n" if lines else b"")
        return _sha256_bytes(payload), len(lines)
    if body.get("audit_jsonl_path"):
        path = Path(str(body["audit_jsonl_path"]))
        payload = path.read_bytes()
        event_count = len([line for line in payload.splitlines() if line.strip()])
        return _sha256_bytes(payload), event_count
    raise ValueError("日报必须提供 audit_hash + audit_event_count，或 audit_events/audit_jsonl_path")


def _prefix_warning(value: str, prefixes: tuple[str, ...], field: str) -> str | None:
    if not value.startswith(prefixes):
        return f"{field} 不是 V42 信任前缀，将由 V42 继续阻塞：{value}"
    if "mock" in value.lower() or "pending" in value.lower() or "todo" in value.lower():
        return f"{field} 包含 mock/pending/todo，将由 V42 继续阻塞：{value}"
    return None


def canonicalize_daily_report(raw: dict[str, Any], *, source_path: Path | None = None) -> tuple[dict[str, Any], list[str]]:
    """Normalize one daily report into the V42 ledger schema."""

    if bool(raw.get("template_only")) or bool(raw.get("example_only")):
        raise ValueError("模板或示例日报不能写入 V45 证据台账")
    sensitive_blockers = _scan_sensitive_material(raw)
    if sensitive_blockers:
        raise ValueError("; ".join(sensitive_blockers))
    body = _raw_daily_body(raw)
    if bool(body.get("template_only")) or bool(body.get("example_only")):
        raise ValueError("模板或示例日报不能写入 V45 证据台账")
    audit_hash, audit_event_count = _audit_hash_from_raw(body)
    row = {
        "date": _as_date(body.get("date")),
        "paper_daily_return": _as_float(body.get("paper_daily_return", body.get("daily_return")), "paper_daily_return"),
        "backtest_daily_return": _as_float(
            body.get("backtest_daily_return", body.get("expected_daily_return")),
            "backtest_daily_return",
        ),
        "actual_slippage_bps": _as_float(body.get("actual_slippage_bps"), "actual_slippage_bps"),
        "expected_slippage_bps": _as_float(body.get("expected_slippage_bps"), "expected_slippage_bps"),
        "actual_cost_bps": _as_float(body.get("actual_cost_bps"), "actual_cost_bps"),
        "expected_cost_bps": _as_float(body.get("expected_cost_bps"), "expected_cost_bps"),
        "risk_capacity_violations": _as_int(body.get("risk_capacity_violations", 0), "risk_capacity_violations"),
        "invariant_violations": _as_int(body.get("invariant_violations", 0), "invariant_violations"),
        "audit_hash": audit_hash,
        "audit_event_count": audit_event_count,
        "archive_ref": _string_field(body, "archive_ref"),
        "market_data_ref": _string_field(body, "market_data_ref"),
        "position_snapshot_ref": _string_field(body, "position_snapshot_ref"),
        "order_fill_log_ref": _string_field(body, "order_fill_log_ref"),
        "auto_trade_enabled": _as_bool(body.get("auto_trade_enabled", False), "auto_trade_enabled"),
        "live_order_submission_allowed": _as_bool(
            body.get("live_order_submission_allowed", False),
            "live_order_submission_allowed",
        ),
    }
    if row["auto_trade_enabled"] or row["live_order_submission_allowed"]:
        raise ValueError("影子模拟盘日报禁止开启自动交易或实盘下单权限")
    warnings = [
        warning
        for warning in (
            _prefix_warning(row["archive_ref"], TRUSTED_ARCHIVE_PREFIXES, "archive_ref"),
            _prefix_warning(row["market_data_ref"], TRUSTED_PROVIDER_PREFIXES, "market_data_ref"),
            _prefix_warning(row["position_snapshot_ref"], TRUSTED_BROKER_PREFIXES, "position_snapshot_ref"),
            _prefix_warning(row["order_fill_log_ref"], TRUSTED_BROKER_PREFIXES, "order_fill_log_ref"),
        )
        if warning is not None
    ]
    row["report_sha256"] = _sha256_bytes(_canonical_json(row).encode("utf-8"))
    row["ingested_at"] = datetime.now(timezone.utc).isoformat()
    if source_path is not None:
        row["source_path"] = str(source_path)
        row["source_sha256"] = _sha256_file(source_path)
    return row, warnings


def default_ledger() -> dict[str, Any]:
    return {
        "version": "V45-paper-shadow-ledger",
        "generated_by": "scripts/ingest_v45_paper_shadow_daily_report.py",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "external_refs": {},
        "daily_reports": [],
    }


def load_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return default_ledger()
    data = _load_json_object(path)
    data.setdefault("version", "V45-paper-shadow-ledger")
    data.setdefault("external_refs", {})
    data.setdefault("daily_reports", [])
    if not isinstance(data["daily_reports"], list):
        raise RuntimeError("ledger daily_reports must be a list")
    if not isinstance(data["external_refs"], dict):
        raise RuntimeError("ledger external_refs must be an object")
    return data


def merge_external_refs(ledger: dict[str, Any], raw: dict[str, Any] | None) -> None:
    if not raw:
        return
    refs = raw.get("external_refs", {})
    if not isinstance(refs, dict):
        raise ValueError("external_refs 必须是对象")
    for key, value in refs.items():
        text = str(value).strip()
        if text:
            ledger["external_refs"][str(key)] = text


def upsert_daily_report(
    ledger: dict[str, Any],
    row: dict[str, Any],
    *,
    allow_replace: bool = False,
) -> str:
    reports = list(ledger.get("daily_reports", []))
    for index, existing in enumerate(reports):
        if isinstance(existing, dict) and existing.get("date") == row["date"]:
            if existing.get("report_sha256") == row["report_sha256"]:
                return "unchanged"
            if not allow_replace:
                raise ValueError(f"日报日期重复且内容不同，需显式 --allow-replace：{row['date']}")
            reports[index] = row
            ledger["daily_reports"] = sorted(reports, key=lambda item: str(item.get("date", "")))
            ledger["updated_at"] = datetime.now(timezone.utc).isoformat()
            return "replaced"
    reports.append(row)
    ledger["daily_reports"] = sorted(reports, key=lambda item: str(item.get("date", "")))
    ledger["updated_at"] = datetime.now(timezone.utc).isoformat()
    return "inserted"


def _load_evidence(evidence_file: str) -> dict[str, str]:
    raw_env = os.getenv("QUANT_PRODUCTION_EVIDENCE_JSON", "").strip()
    if raw_env:
        loaded = json.loads(raw_env)
    elif evidence_file:
        loaded = json.loads(Path(evidence_file).read_text(encoding="utf-8"))
    else:
        loaded = {}
    if not isinstance(loaded, dict):
        raise RuntimeError("production evidence must be a JSON object")
    return {str(key): str(value).strip() for key, value in loaded.items()}


def refresh_v42_v44(
    *,
    ledger: dict[str, Any],
    v40_package_path: Path,
    v42_output_path: Path,
    v42_report_path: Path,
    v43_gate_path: Path,
    v44_output_path: Path,
    v44_report_path: Path,
    evidence: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    v42_module = _load_script_module(
        "validate_v42_paper_shadow_evidence_runtime",
        PROJECT_DIR / "scripts" / "validate_v42_paper_shadow_evidence.py",
    )
    v44_module = _load_script_module(
        "prepare_v44_production_readiness_gate_runtime",
        PROJECT_DIR / "scripts" / "prepare_v44_production_readiness_gate.py",
    )
    v40_package = _load_json_object(v40_package_path)
    v42_payload = v42_module.build_evidence_gate(v40_package, ledger)
    v42_output_path.parent.mkdir(parents=True, exist_ok=True)
    v42_output_path.write_text(json.dumps(v42_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    v42_module._write_report(v42_report_path, v42_payload)

    v43_gate = _load_json_object(v43_gate_path)
    v44_payload = v44_module.build_readiness_gate(
        v40_package=v40_package,
        v42_gate=v42_payload,
        v43_gate=v43_gate,
        evidence=evidence,
    )
    v44_output_path.parent.mkdir(parents=True, exist_ok=True)
    v44_output_path.write_text(json.dumps(v44_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    v44_module._write_report(v44_report_path, v44_payload)
    return v42_payload, v44_payload


def build_summary(
    *,
    ledger_path: Path,
    ledger: dict[str, Any],
    action: str,
    warnings: list[str],
    v42_payload: dict[str, Any],
    v44_payload: dict[str, Any],
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or datetime.now(timezone.utc)
    return {
        "ts": generated_at.isoformat(),
        "version": "V45-paper-shadow-daily-ingestion",
        "production_ready": False,
        "ledger_path": str(ledger_path),
        "daily_report_count": len(ledger.get("daily_reports", [])),
        "ingestion_action": action,
        "warnings": warnings,
        "v42_paper_evidence_ready": bool(v42_payload.get("paper_evidence_ready")),
        "v44_production_ready": bool(v44_payload.get("production_ready")),
        "v44_blocker_count": len(v44_payload.get("production_blockers", [])),
        "latest_report_date": (
            ledger["daily_reports"][-1]["date"] if ledger.get("daily_reports") else None
        ),
        "next_actions": [
            "每日收盘后导出只读行情、模拟成交、持仓快照和审计事件，形成单日 JSON 并运行本脚本。",
            "连续至少 90 个自然日、至少 60 个日报交易日后，V42 才可能解除日报数量阻塞。",
            "日报不得包含 token、API key、password 或私钥值。",
            "即使 V42 通过，也必须等待 V40 robustness、外部 evidence 和风控审批全部通过。",
        ],
    }


def _write_report(path: Path, summary: dict[str, Any]) -> None:
    action_text = {
        "refreshed_without_new_report": "未导入新日报，仅刷新门禁",
        "inserted": "新增日报",
        "replaced": "替换既有日报",
        "unchanged": "日报内容未变化",
    }.get(str(summary["ingestion_action"]), str(summary["ingestion_action"]))
    lines = [
        "# V45 影子模拟盘日报导入流水线",
        "",
        "状态：流水线已生成，当前不代表生产批准。",
        "",
        "## 结论",
        "",
        "- `production_ready=false`",
        f"- 日报数量：{summary['daily_report_count']}",
        f"- 本次动作：{action_text}",
        f"- V42 影子模拟盘证据通过：{str(summary['v42_paper_evidence_ready']).lower()}",
        f"- V44 生产准入通过：{str(summary['v44_production_ready']).lower()}",
        f"- V44 阻塞项数量：{summary['v44_blocker_count']}",
        f"- 最新日报日期：{summary['latest_report_date'] or '无'}",
        "",
        "## 台账",
        "",
        f"- `{summary['ledger_path']}`",
        "",
        "## 警告",
        "",
    ]
    if summary["warnings"]:
        lines.extend(f"- {item}" for item in summary["warnings"])
    else:
        lines.append("- 无")
    lines.extend(["", "## 下一步", ""])
    lines.extend(f"- {item}" for item in summary["next_actions"])
    lines.extend(
        [
            "",
            "## 日报最小字段",
            "",
            "- `date`、`paper_daily_return`、`backtest_daily_return`",
            "- `actual_slippage_bps`、`expected_slippage_bps`、`actual_cost_bps`、`expected_cost_bps`",
            "- `audit_hash` 与 `audit_event_count`，或 `audit_events` / `audit_jsonl_path`",
            "- `archive_ref`、`market_data_ref`、`position_snapshot_ref`、`order_fill_log_ref`",
            "- `auto_trade_enabled=false`、`live_order_submission_allowed=false`",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def run_ingestion(
    *,
    daily_report_path: Path | None,
    allow_replace: bool,
    ledger_path: Path,
    summary_path: Path,
    report_path: Path,
    v40_package_path: Path,
    v42_output_path: Path,
    v42_report_path: Path,
    v43_gate_path: Path,
    v44_output_path: Path,
    v44_report_path: Path,
    evidence: dict[str, str],
) -> dict[str, Any]:
    ledger = load_ledger(ledger_path)
    warnings: list[str] = []
    action = "refreshed_without_new_report"
    raw_report: dict[str, Any] | None = None
    if daily_report_path is not None:
        raw_report = _load_json_object(daily_report_path)
        row, warnings = canonicalize_daily_report(raw_report, source_path=daily_report_path)
        merge_external_refs(ledger, raw_report)
        action = upsert_daily_report(ledger, row, allow_replace=allow_replace)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    v42_payload, v44_payload = refresh_v42_v44(
        ledger=ledger,
        v40_package_path=v40_package_path,
        v42_output_path=v42_output_path,
        v42_report_path=v42_report_path,
        v43_gate_path=v43_gate_path,
        v44_output_path=v44_output_path,
        v44_report_path=v44_report_path,
        evidence=evidence,
    )
    summary = build_summary(
        ledger_path=ledger_path,
        ledger=ledger,
        action=action,
        warnings=warnings,
        v42_payload=v42_payload,
        v44_payload=v44_payload,
    )
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(report_path, summary)
    return summary


def main() -> None:
    args = _parse_args()
    daily_report_path = Path(args.daily_report_json) if args.daily_report_json else None
    summary = run_ingestion(
        daily_report_path=daily_report_path,
        allow_replace=args.allow_replace,
        ledger_path=Path(args.ledger_json),
        summary_path=Path(args.summary_json),
        report_path=Path(args.report_md),
        v40_package_path=Path(args.v40_package_json),
        v42_output_path=Path(args.v42_output_json),
        v42_report_path=Path(args.v42_report_md),
        v43_gate_path=Path(args.v43_gate_json),
        v44_output_path=Path(args.v44_output_json),
        v44_report_path=Path(args.v44_report_md),
        evidence=_load_evidence(str(args.evidence_file)),
    )
    print(
        json.dumps(
            {
                "production_ready": False,
                "daily_report_count": summary["daily_report_count"],
                "ingestion_action": summary["ingestion_action"],
                "v42_paper_evidence_ready": summary["v42_paper_evidence_ready"],
                "v44_production_ready": summary["v44_production_ready"],
                "v44_blocker_count": summary["v44_blocker_count"],
                "summary": str(Path(args.summary_json)),
                "report": str(Path(args.report_md)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.require_paper_evidence_ready and not summary["v42_paper_evidence_ready"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
