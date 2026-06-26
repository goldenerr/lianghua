#!/usr/bin/env python3
"""Classify V29 recent-90 weakness without parameter tuning.

Consumes the detailed V29 recent-90 regime diagnostic and produces a compact
failure-mode report. This is a decision-control artifact: it identifies whether
weakness is mostly stock-alpha sleeve decay, regime detection lag, defensive
sleeve under-offset, cost drag, or drawdown scaling. It does not tune or change
strategy parameters.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _paths import RESULTS_DIR

DEFAULT_INPUT = RESULTS_DIR / "quant_v29_recent90_regime_diagnostic.json"
DEFAULT_OUTPUT = RESULTS_DIR / "quant_v29_recent90_failure_mode_report.json"
DEFAULT_REPORT = Path("docs/research/quant_v29_recent90_failure_mode_report.md")
STOCK_CONTRIB_PREFIX = "contrib_price_"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-json", default=str(DEFAULT_INPUT))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--report-md", default=str(DEFAULT_REPORT))
    return parser.parse_args()


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"JSON must be an object: {path}")
    return data


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _sum_stock_alpha(contribs: dict[str, Any]) -> float:
    return sum(
        _safe_float(value)
        for key, value in contribs.items()
        if key.startswith(STOCK_CONTRIB_PREFIX)
    )


def _worst_month(monthly: list[dict[str, Any]]) -> dict[str, Any]:
    if not monthly:
        return {}
    return min(monthly, key=lambda item: _safe_float(item.get("return")))


def _negative_sources(contribs: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        {"source": key, "contribution_sum": round(_safe_float(value), 6)}
        for key, value in contribs.items()
        if _safe_float(value) < 0
    ]
    rows.sort(key=lambda item: item["contribution_sum"])
    return rows


def classify_candidate(summary: dict[str, Any]) -> dict[str, Any]:
    metrics = summary.get("recent_metrics", {})
    contribs = summary.get("contribution_totals_recent", {})
    monthly = summary.get("monthly_recent", [])
    if not isinstance(metrics, dict):
        metrics = {}
    if not isinstance(contribs, dict):
        contribs = {}
    if not isinstance(monthly, list):
        monthly = []
    stock_alpha = _sum_stock_alpha(contribs)
    crisis = _safe_float(contribs.get("crisis_contrib"))
    carry = _safe_float(contribs.get("carry_contrib"))
    total_return = _safe_float(metrics.get("total_return"))
    avg_stock_exposure = _safe_float(summary.get("avg_stock_exposure_recent"))
    risk_off_ratio = _safe_float(summary.get("risk_off_ratio_recent"))
    worst = _worst_month([row for row in monthly if isinstance(row, dict)])
    worst_return = _safe_float(worst.get("return")) if worst else 0.0
    worst_risk_off = _safe_float(worst.get("risk_off_ratio")) if worst else 0.0
    worst_stock_exposure = _safe_float(worst.get("avg_stock_exposure")) if worst else 0.0
    mdd_counts = summary.get("mdd_state_counts_recent", {})
    reduce_days = (
        int(_safe_float(mdd_counts.get("reduce_scale"))) if isinstance(mdd_counts, dict) else 0
    )
    stop_days = (
        int(_safe_float(mdd_counts.get("stop_scale"))) if isinstance(mdd_counts, dict) else 0
    )
    recent_days = int(_safe_float(summary.get("recent_days")))
    reduce_ratio = (reduce_days + stop_days) / recent_days if recent_days else 0.0

    failure_modes: list[str] = []
    if total_return >= 0:
        return {
            "candidate": summary.get("candidate"),
            "recent_total_return": round(total_return, 6),
            "recent_sharpe": _safe_float(metrics.get("sharpe_ratio")),
            "recent_mdd": _safe_float(metrics.get("max_drawdown")),
            "stock_alpha_contribution": round(stock_alpha, 6),
            "crisis_contribution": round(crisis, 6),
            "carry_contribution": round(carry, 6),
            "avg_stock_exposure": round(avg_stock_exposure, 6),
            "risk_off_ratio": round(risk_off_ratio, 6),
            "drawdown_scaling_ratio": round(reduce_ratio, 6),
            "worst_month": worst,
            "negative_sources": _negative_sources(contribs),
            "failure_modes": ["recent_window_not_negative"],
            "recommended_next_work": [
                "Continue paper evidence accumulation and rerun diagnostics after the next full month."
            ],
            "production_ready": False,
            "not_parameter_tuning": True,
        }
    if total_return < 0 and stock_alpha < 0 and abs(stock_alpha) >= abs(total_return) * 0.6:
        failure_modes.append("stock_alpha_sleeve_decay")
    if total_return < 0 and crisis + carry >= 0 and abs(crisis + carry) < abs(stock_alpha) * 0.25:
        failure_modes.append("defensive_sleeve_under_offset")
    if (
        worst_return < -0.015
        and worst_risk_off < 0.2
        and worst_stock_exposure > avg_stock_exposure * 1.2
    ):
        failure_modes.append("regime_detection_lag_or_under_de_risking")
    if reduce_ratio > 0.15:
        failure_modes.append("drawdown_scaling_active_after_damage")
    cost_drag = sum(_safe_float(row.get("cost_sum")) for row in monthly if isinstance(row, dict))
    if total_return < 0 and cost_drag > abs(total_return) * 0.2:
        failure_modes.append("cost_drag_material")
    if not failure_modes and total_return < 0:
        failure_modes.append("unclassified_negative_recent_window")

    recommended_next_work = []
    if "stock_alpha_sleeve_decay" in failure_modes:
        recommended_next_work.append(
            "Run signal-decay diagnostics by sleeve and month before any parameter changes."
        )
    if "regime_detection_lag_or_under_de_risking" in failure_modes:
        recommended_next_work.append(
            "Audit risk-off trigger timing around the worst month; compare signal date vs drawdown start without changing thresholds."
        )
    if "defensive_sleeve_under_offset" in failure_modes:
        recommended_next_work.append(
            "Evaluate whether crisis/carry sleeves are true offsets in the current A-share regime; do not simply increase weights."
        )
    if "drawdown_scaling_active_after_damage" in failure_modes:
        recommended_next_work.append(
            "Check whether drawdown scaling reacts after losses rather than before them; keep it as diagnostics only."
        )
    if not recommended_next_work:
        recommended_next_work.append(
            "Continue paper evidence accumulation and rerun diagnostics after the next full month."
        )

    return {
        "candidate": summary.get("candidate"),
        "recent_total_return": round(total_return, 6),
        "recent_sharpe": _safe_float(metrics.get("sharpe_ratio")),
        "recent_mdd": _safe_float(metrics.get("max_drawdown")),
        "stock_alpha_contribution": round(stock_alpha, 6),
        "crisis_contribution": round(crisis, 6),
        "carry_contribution": round(carry, 6),
        "avg_stock_exposure": round(avg_stock_exposure, 6),
        "risk_off_ratio": round(risk_off_ratio, 6),
        "drawdown_scaling_ratio": round(reduce_ratio, 6),
        "worst_month": worst,
        "negative_sources": _negative_sources(contribs),
        "failure_modes": failure_modes,
        "recommended_next_work": recommended_next_work,
        "production_ready": False,
        "not_parameter_tuning": True,
    }


def build_report(diagnostic: dict[str, Any], generated_at: datetime) -> dict[str, Any]:
    summaries = diagnostic.get("summaries", [])
    if not isinstance(summaries, list):
        summaries = []
    classifications = [classify_candidate(item) for item in summaries if isinstance(item, dict)]
    global_modes = sorted({mode for item in classifications for mode in item["failure_modes"]})
    return {
        "ts": generated_at.isoformat(),
        "version": "V29-recent90-failure-mode-report",
        "research_only": True,
        "production_ready": False,
        "not_parameter_tuning": True,
        "input_version": diagnostic.get("version"),
        "candidate_count": len(classifications),
        "global_failure_modes": global_modes,
        "classifications": classifications,
        "decision": {
            "can_claim_industry_leading": False,
            "can_pass_production": False,
            "should_parameter_tune_now": False,
            "next_step_class": "signal/regime/risk diagnostics, not small parameter tuning",
        },
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# V29 recent-90 failure-mode report",
        "",
        "状态：研究诊断，不调参，不解除生产 blocker。",
        "",
        f"- candidate_count: {report['candidate_count']}",
        f"- global_failure_modes: {', '.join(report['global_failure_modes'])}",
        "- can_claim_industry_leading: false",
        "- should_parameter_tune_now: false",
        "",
    ]
    for item in report["classifications"]:
        lines.extend(
            [
                f"## {item['candidate']}",
                "",
                f"- recent_total_return: {item['recent_total_return']}",
                f"- recent_sharpe: {item['recent_sharpe']}",
                f"- recent_mdd: {item['recent_mdd']}",
                f"- stock_alpha_contribution: {item['stock_alpha_contribution']}",
                f"- crisis_contribution: {item['crisis_contribution']}",
                f"- failure_modes: {', '.join(item['failure_modes'])}",
                f"- worst_month: {item['worst_month'].get('month') if isinstance(item['worst_month'], dict) else None}",
                "",
                "### Recommended next work",
                "",
            ]
        )
        lines.extend(f"- {step}" for step in item["recommended_next_work"])
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    report = build_report(_load_json(Path(args.input_json)), datetime.now(timezone.utc))
    _write_json(Path(args.output_json), report)
    _write_markdown(Path(args.report_md), report)
    print(
        json.dumps(
            {
                "production_ready": False,
                "not_parameter_tuning": True,
                "candidate_count": report["candidate_count"],
                "global_failure_modes": report["global_failure_modes"],
                "output": args.output_json,
                "report": args.report_md,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
