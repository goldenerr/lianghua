#!/usr/bin/env python3
"""Validate V40 paper-shadow daily evidence without enabling production."""

from __future__ import annotations

import argparse
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from _paths import RESULTS_DIR

DEFAULT_PACKAGE = RESULTS_DIR / "quant_v40_paper_shadow_package.json"
DEFAULT_OUTPUT = RESULTS_DIR / "quant_v42_paper_shadow_evidence_gate.json"
DEFAULT_REPORT = Path("docs/research/quant_v42_paper_shadow_evidence_gate.md")

HASH_RE = re.compile(r"^[0-9a-f]{64}$")
TRUSTED_ARCHIVE_PREFIXES = ("worm://", "archive://", "s3://worm-")
TRUSTED_PROVIDER_PREFIXES = ("provider://", "vendor://")
TRUSTED_POSITION_PREFIXES = ("broker://", "exchange://")
SENSITIVE_KEY_RE = re.compile(r"(^|[_-])(token|password|api[_-]?key|private[_-]?key)([_-]|$)", re.I)
SENSITIVE_VALUE_RE = re.compile(r"(Bearer\s+[A-Za-z0-9._=-]+|sk-[A-Za-z0-9]{12,}|AKIA[0-9A-Z]{16})")


@dataclass(frozen=True)
class EvidenceThresholds:
    min_calendar_days: int = 90
    min_report_days: int = 60
    max_abs_cumulative_return_drift: float = 0.05
    max_mean_abs_daily_return_drift_bps: float = 5.0
    max_slippage_over_expected_bps: float = 5.0
    max_cost_over_expected_bps: float = 5.0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-json", default=str(DEFAULT_PACKAGE))
    parser.add_argument(
        "--paper-ledger-json",
        default="",
        help="Optional JSON evidence ledger exported by the paper-shadow runner.",
    )
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--report-md", default=str(DEFAULT_REPORT))
    parser.add_argument("--min-calendar-days", type=int, default=90)
    parser.add_argument("--min-report-days", type=int, default=60)
    parser.add_argument("--max-cumulative-drift", type=float, default=0.05)
    parser.add_argument("--max-daily-drift-bps", type=float, default=5.0)
    parser.add_argument("--max-slippage-excess-bps", type=float, default=5.0)
    parser.add_argument("--max-cost-excess-bps", type=float, default=5.0)
    parser.add_argument(
        "--require-paper-evidence-ready",
        action="store_true",
        help="Exit non-zero unless the paper-shadow evidence gate passes.",
    )
    return parser.parse_args()


def _load_json_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"JSON must be an object: {path}")
    return data


def _finite_float(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def _int_like(value: Any, field: str) -> int:
    result = _finite_float(value, field)
    if abs(result - round(result)) > 1e-9:
        raise ValueError(f"{field} must be an integer")
    return int(round(result))


def _parse_date(value: Any) -> date:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("date must be a non-empty YYYY-MM-DD string")
    return date.fromisoformat(value.strip())


def _trusted_ref(value: Any, prefixes: Sequence[str]) -> bool:
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    return bool(stripped) and stripped.startswith(tuple(prefixes)) and "mock" not in stripped.lower()


def _daily_return(row: Mapping[str, Any], field: str, fallback: str) -> float:
    if field in row:
        return _finite_float(row[field], field)
    return _finite_float(row.get(fallback), fallback)


def _scan_sensitive_material(node: Any, path: str = "$") -> list[str]:
    blockers: list[str] = []
    if isinstance(node, Mapping):
        for raw_key, value in node.items():
            key = str(raw_key)
            child_path = f"{path}.{key}"
            if SENSITIVE_KEY_RE.search(key):
                blockers.append(f"sensitive-looking key is not allowed in evidence: {child_path}")
            blockers.extend(_scan_sensitive_material(value, child_path))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            blockers.extend(_scan_sensitive_material(value, f"{path}[{index}]"))
    elif isinstance(node, str) and SENSITIVE_VALUE_RE.search(node):
        blockers.append(f"sensitive-looking value is not allowed in evidence: {path}")
    return blockers


def _candidate_controls_are_closed(package: Mapping[str, Any]) -> list[str]:
    candidate = package.get("candidate", {})
    if not isinstance(candidate, Mapping):
        return ["paper-shadow package missing candidate controls"]
    blockers: list[str] = []
    if bool(candidate.get("auto_trade_enabled")):
        blockers.append("package candidate auto_trade_enabled must remain false")
    if bool(candidate.get("live_order_submission_allowed")):
        blockers.append("package candidate live_order_submission_allowed must remain false")
    if _finite_float(candidate.get("max_live_capital_fraction", 1), "max_live_capital_fraction") != 0:
        blockers.append("package candidate max_live_capital_fraction must remain 0.0")
    return blockers


def _validate_daily_rows(
    rows: Sequence[Any],
    thresholds: EvidenceThresholds,
) -> tuple[dict[str, Any], list[str]]:
    blockers: list[str] = []
    metrics: dict[str, Any] = {
        "report_days": 0,
        "calendar_days": 0,
        "paper_cumulative_return": 0.0,
        "backtest_cumulative_return": 0.0,
        "cumulative_return_drift": 0.0,
        "mean_abs_daily_return_drift_bps": None,
        "mean_actual_slippage_bps": None,
        "mean_expected_slippage_bps": None,
        "mean_actual_cost_bps": None,
        "mean_expected_cost_bps": None,
    }
    if not rows:
        return metrics, ["missing daily paper-shadow reports"]

    parsed_rows: list[tuple[date, Mapping[str, Any]]] = []
    seen_dates: set[date] = set()
    for index, raw_row in enumerate(rows):
        if not isinstance(raw_row, Mapping):
            blockers.append(f"daily_reports[{index}] must be an object")
            continue
        try:
            report_date = _parse_date(raw_row.get("date"))
        except ValueError as exc:
            blockers.append(f"daily_reports[{index}].date invalid: {exc}")
            continue
        if report_date in seen_dates:
            blockers.append(f"duplicate paper-shadow report date: {report_date.isoformat()}")
        seen_dates.add(report_date)
        parsed_rows.append((report_date, raw_row))

    parsed_rows.sort(key=lambda item: item[0])
    if not parsed_rows:
        return metrics, blockers

    report_days = len(parsed_rows)
    calendar_days = (parsed_rows[-1][0] - parsed_rows[0][0]).days + 1
    metrics["report_days"] = report_days
    metrics["calendar_days"] = calendar_days
    if report_days < thresholds.min_report_days:
        blockers.append(
            f"paper-shadow report days {report_days} < required {thresholds.min_report_days}"
        )
    if calendar_days < thresholds.min_calendar_days:
        blockers.append(
            f"paper-shadow calendar span {calendar_days} < required {thresholds.min_calendar_days}"
        )

    paper_growth = 1.0
    backtest_growth = 1.0
    daily_drift_bps: list[float] = []
    actual_slippage: list[float] = []
    expected_slippage: list[float] = []
    actual_cost: list[float] = []
    expected_cost: list[float] = []

    for report_date, row in parsed_rows:
        date_label = report_date.isoformat()
        try:
            if bool(row.get("auto_trade_enabled")):
                blockers.append(f"{date_label}: auto_trade_enabled must be false")
            if bool(row.get("live_order_submission_allowed")):
                blockers.append(f"{date_label}: live_order_submission_allowed must be false")
            if _int_like(row.get("risk_capacity_violations", 0), "risk_capacity_violations") != 0:
                blockers.append(f"{date_label}: risk_capacity_violations must be zero")
            if _int_like(row.get("invariant_violations", 0), "invariant_violations") != 0:
                blockers.append(f"{date_label}: invariant_violations must be zero")

            audit_hash = str(row.get("audit_hash", "")).strip().lower()
            if not HASH_RE.match(audit_hash):
                blockers.append(f"{date_label}: audit_hash must be a sha256 hex digest")
            if _int_like(row.get("audit_event_count", 0), "audit_event_count") <= 0:
                blockers.append(f"{date_label}: audit_event_count must be positive")
            if not _trusted_ref(row.get("archive_ref"), TRUSTED_ARCHIVE_PREFIXES):
                blockers.append(f"{date_label}: archive_ref must be trusted WORM/archive evidence")
            if not _trusted_ref(row.get("market_data_ref"), TRUSTED_PROVIDER_PREFIXES):
                blockers.append(f"{date_label}: market_data_ref must be trusted provider evidence")
            if not _trusted_ref(row.get("position_snapshot_ref"), TRUSTED_POSITION_PREFIXES):
                blockers.append(
                    f"{date_label}: position_snapshot_ref must be broker/exchange-backed"
                )
            if not _trusted_ref(row.get("order_fill_log_ref"), TRUSTED_POSITION_PREFIXES):
                blockers.append(f"{date_label}: order_fill_log_ref must be broker/exchange-backed")

            paper_return = _daily_return(row, "paper_daily_return", "daily_return")
            backtest_return = _daily_return(row, "backtest_daily_return", "expected_daily_return")
            paper_growth *= 1.0 + paper_return
            backtest_growth *= 1.0 + backtest_return
            daily_drift_bps.append(abs(paper_return - backtest_return) * 10000.0)

            actual_slippage.append(_finite_float(row.get("actual_slippage_bps"), "actual_slippage_bps"))
            expected_slippage.append(
                _finite_float(row.get("expected_slippage_bps"), "expected_slippage_bps")
            )
            actual_cost.append(_finite_float(row.get("actual_cost_bps"), "actual_cost_bps"))
            expected_cost.append(_finite_float(row.get("expected_cost_bps"), "expected_cost_bps"))
        except ValueError as exc:
            blockers.append(f"{date_label}: {exc}")

    paper_cumulative = paper_growth - 1.0
    backtest_cumulative = backtest_growth - 1.0
    cumulative_drift = paper_cumulative - backtest_cumulative
    metrics["paper_cumulative_return"] = round(paper_cumulative, 8)
    metrics["backtest_cumulative_return"] = round(backtest_cumulative, 8)
    metrics["cumulative_return_drift"] = round(cumulative_drift, 8)

    if daily_drift_bps:
        metrics["mean_abs_daily_return_drift_bps"] = round(
            sum(daily_drift_bps) / len(daily_drift_bps), 6
        )
        if metrics["mean_abs_daily_return_drift_bps"] > thresholds.max_mean_abs_daily_return_drift_bps:
            blockers.append(
                "mean daily return drift "
                f"{metrics['mean_abs_daily_return_drift_bps']:.4f}bps exceeds "
                f"{thresholds.max_mean_abs_daily_return_drift_bps:.4f}bps"
            )
    if abs(cumulative_drift) > thresholds.max_abs_cumulative_return_drift:
        blockers.append(
            f"cumulative return drift {cumulative_drift:.4%} exceeds "
            f"{thresholds.max_abs_cumulative_return_drift:.4%}"
        )

    if actual_slippage and expected_slippage:
        mean_actual = sum(actual_slippage) / len(actual_slippage)
        mean_expected = sum(expected_slippage) / len(expected_slippage)
        metrics["mean_actual_slippage_bps"] = round(mean_actual, 6)
        metrics["mean_expected_slippage_bps"] = round(mean_expected, 6)
        if mean_actual - mean_expected > thresholds.max_slippage_over_expected_bps:
            blockers.append(
                f"slippage excess {mean_actual - mean_expected:.4f}bps exceeds "
                f"{thresholds.max_slippage_over_expected_bps:.4f}bps"
            )
    if actual_cost and expected_cost:
        mean_actual_cost = sum(actual_cost) / len(actual_cost)
        mean_expected_cost = sum(expected_cost) / len(expected_cost)
        metrics["mean_actual_cost_bps"] = round(mean_actual_cost, 6)
        metrics["mean_expected_cost_bps"] = round(mean_expected_cost, 6)
        if mean_actual_cost - mean_expected_cost > thresholds.max_cost_over_expected_bps:
            blockers.append(
                f"cost excess {mean_actual_cost - mean_expected_cost:.4f}bps exceeds "
                f"{thresholds.max_cost_over_expected_bps:.4f}bps"
            )
    return metrics, blockers


def build_evidence_gate(
    package: Mapping[str, Any],
    ledger: Mapping[str, Any] | None,
    thresholds: EvidenceThresholds | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a fail-closed evidence report for the V40 paper-shadow candidate."""

    thresholds = thresholds or EvidenceThresholds()
    generated_at = generated_at or datetime.now(timezone.utc)
    blockers: list[str] = []
    ledger = ledger or {}

    blockers.extend(_scan_sensitive_material(package))
    blockers.extend(_scan_sensitive_material(ledger))
    blockers.extend(_candidate_controls_are_closed(package))

    if package.get("shadow_candidate_exists") is not True:
        blockers.append("V40 paper-shadow candidate does not exist")

    package_blockers = list(package.get("blockers", []))
    if package_blockers:
        blockers.append(f"paper-shadow package still has {len(package_blockers)} blocker(s)")

    if not ledger:
        daily_metrics, daily_blockers = _validate_daily_rows([], thresholds)
        blockers.extend(daily_blockers)
    else:
        rows = ledger.get("daily_reports", [])
        if not isinstance(rows, list):
            rows = []
            blockers.append("paper ledger daily_reports must be a list")
        daily_metrics, daily_blockers = _validate_daily_rows(rows, thresholds)
        blockers.extend(daily_blockers)

    external_refs = ledger.get("external_refs", {}) if isinstance(ledger, Mapping) else {}
    if not isinstance(external_refs, Mapping):
        external_refs = {}
        blockers.append("paper ledger external_refs must be an object")
    paper_ref = str(external_refs.get("paper_trading_90d", "")).strip()
    if not _trusted_ref(paper_ref, ("paper://", "broker://", "archive://", "worm://")):
        blockers.append("missing trusted paper_trading_90d evidence ref")

    paper_evidence_ready = not blockers
    return {
        "ts": generated_at.isoformat(),
        "version": "V42-paper-shadow-evidence-gate",
        "research_only": False,
        "production_ready": False,
        "paper_evidence_ready": paper_evidence_ready,
        "paper_shadow_candidate_exists": bool(package.get("shadow_candidate_exists")),
        "thresholds": {
            "min_calendar_days": thresholds.min_calendar_days,
            "min_report_days": thresholds.min_report_days,
            "max_abs_cumulative_return_drift": thresholds.max_abs_cumulative_return_drift,
            "max_mean_abs_daily_return_drift_bps": thresholds.max_mean_abs_daily_return_drift_bps,
            "max_slippage_over_expected_bps": thresholds.max_slippage_over_expected_bps,
            "max_cost_over_expected_bps": thresholds.max_cost_over_expected_bps,
        },
        "metrics": daily_metrics,
        "blockers": blockers,
        "package_blocker_count": len(package_blockers),
        "note": (
            "This gate only evaluates paper-shadow evidence. Production remains false until "
            "external production evidence, approvals, paper acceptance and operational gates pass."
        ),
    }


def _format_metric(value: Any) -> str:
    return "无" if value is None else str(value)


def _zh_blocker(item: str) -> str:
    if item.startswith("paper-shadow package still has "):
        count = item.removeprefix("paper-shadow package still has ").split(" ", maxsplit=1)[0]
        return f"V40 影子模拟盘包仍有 {count} 个阻塞项"
    translations = {
        "missing daily paper-shadow reports": "缺少每日影子模拟盘报告",
        "missing trusted paper_trading_90d evidence ref": "缺少可信 paper_trading_90d 证据引用",
        "V40 paper-shadow candidate does not exist": "V40 影子模拟盘候选不存在",
        "paper-shadow package missing candidate controls": "影子模拟盘包缺少候选控制字段",
        "package candidate auto_trade_enabled must remain false": "候选 auto_trade_enabled 必须保持 false",
        "package candidate live_order_submission_allowed must remain false": (
            "候选 live_order_submission_allowed 必须保持 false"
        ),
        "package candidate max_live_capital_fraction must remain 0.0": (
            "候选 max_live_capital_fraction 必须保持 0.0"
        ),
        "paper ledger daily_reports must be a list": "模拟盘台账 daily_reports 必须是列表",
        "paper ledger external_refs must be an object": "模拟盘台账 external_refs 必须是对象",
    }
    return translations.get(item, item)


def _write_report(path: Path, payload: Mapping[str, Any]) -> None:
    metrics = payload["metrics"]
    lines = [
        "# V42 影子模拟盘证据门禁",
        "",
        "状态：本地证据门禁已实现，当前未解除生产阻塞。",
        "",
        "## 结论",
        "",
        f"- `paper_evidence_ready={str(payload['paper_evidence_ready']).lower()}`",
        "- `production_ready=false`",
        f"- 包内剩余阻塞项数量：{payload['package_blocker_count']}",
        f"- 当前门禁阻塞项数量：{len(payload['blockers'])}",
        "",
        "## 当前观测",
        "",
        f"- 报告交易日数：{metrics['report_days']}",
        f"- 覆盖自然日跨度：{metrics['calendar_days']}",
        f"- paper 累计收益：{metrics['paper_cumulative_return']:.4%}",
        f"- 对齐回测累计收益：{metrics['backtest_cumulative_return']:.4%}",
        f"- 累计收益漂移：{metrics['cumulative_return_drift']:.4%}",
        f"- 平均日收益漂移：{_format_metric(metrics['mean_abs_daily_return_drift_bps'])}",
        f"- 平均实际滑点 bps：{_format_metric(metrics['mean_actual_slippage_bps'])}",
        f"- 平均预期滑点 bps：{_format_metric(metrics['mean_expected_slippage_bps'])}",
        f"- 平均实际成本 bps：{_format_metric(metrics['mean_actual_cost_bps'])}",
        f"- 平均预期成本 bps：{_format_metric(metrics['mean_expected_cost_bps'])}",
        "",
        "## 阻塞项",
        "",
    ]
    if payload["blockers"]:
        lines.extend(f"- {_zh_blocker(str(item))}" for item in payload["blockers"])
    else:
        lines.append("- 无")
    lines.extend(
        [
            "",
            "## 门禁说明",
            "",
            "- 每日影子模拟盘记录必须包含审计 hash、归档引用、行情引用、持仓快照引用和订单/成交日志引用。",
            "- 自动交易和实盘下单权限必须保持关闭。",
            "- 任何风控容量违规、不变式违规、敏感信息泄漏、日报缺失或漂移超限都会阻塞。",
            "- 该门禁只判断影子模拟盘证据是否足够，不代表生产批准。",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    thresholds = EvidenceThresholds(
        min_calendar_days=args.min_calendar_days,
        min_report_days=args.min_report_days,
        max_abs_cumulative_return_drift=args.max_cumulative_drift,
        max_mean_abs_daily_return_drift_bps=args.max_daily_drift_bps,
        max_slippage_over_expected_bps=args.max_slippage_excess_bps,
        max_cost_over_expected_bps=args.max_cost_excess_bps,
    )
    package = _load_json_object(Path(args.package_json))
    ledger = _load_json_object(Path(args.paper_ledger_json)) if args.paper_ledger_json else {}
    payload = build_evidence_gate(package, ledger, thresholds)
    output = Path(args.output_json)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(Path(args.report_md), payload)
    print(
        json.dumps(
            {
                "production_ready": payload["production_ready"],
                "paper_evidence_ready": payload["paper_evidence_ready"],
                "blocker_count": len(payload["blockers"]),
                "output": str(output),
                "report": str(Path(args.report_md)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.require_paper_evidence_ready and not payload["paper_evidence_ready"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
