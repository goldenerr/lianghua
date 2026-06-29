#!/usr/bin/env python3
"""Plan V29 recent-90 repair work without tuning production parameters.

This script consumes the existing recent-90 regime, failure-mode and signal-decay
reports, then emits a compact decision matrix for what to investigate next. It is
intentionally diagnostic only: it does not change strategy weights, thresholds or
production readiness gates.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _paths import RESULTS_DIR

DEFAULT_REGIME = RESULTS_DIR / "quant_v29_recent90_regime_diagnostic.json"
DEFAULT_FAILURE = RESULTS_DIR / "quant_v29_recent90_failure_mode_report.json"
DEFAULT_DECAY = RESULTS_DIR / "quant_v29_recent90_signal_decay_report.json"
DEFAULT_OUTPUT = RESULTS_DIR / "quant_v29_recent90_repair_plan.json"
DEFAULT_REPORT = Path("docs/research/quant_v29_recent90_repair_plan.md")
STOCK_PREFIX = "contrib_price_"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regime-json", default=str(DEFAULT_REGIME))
    parser.add_argument("--failure-json", default=str(DEFAULT_FAILURE))
    parser.add_argument("--decay-json", default=str(DEFAULT_DECAY))
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
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out


def _as_dict_by_candidate(rows: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        candidate = row.get("candidate")
        if isinstance(candidate, str) and candidate:
            out[candidate] = row
    return out


def _stock_sum(contribs: dict[str, Any]) -> float:
    return sum(_safe_float(v) for k, v in contribs.items() if k.startswith(STOCK_PREFIX))


def _worst_month(summary: dict[str, Any]) -> dict[str, Any]:
    monthly = summary.get("monthly_recent", [])
    if not isinstance(monthly, list) or not monthly:
        return {}
    rows = [row for row in monthly if isinstance(row, dict)]
    return min(rows, key=lambda row: _safe_float(row.get("return"))) if rows else {}


def _dominant_decay_sleeves(decay_row: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    rows = decay_row.get("dominant_negative_sleeves", [])
    if not isinstance(rows, list):
        return []
    clean = [row for row in rows if isinstance(row, dict)]
    clean.sort(key=lambda row: _safe_float(row.get("recent_sum")))
    return clean[:limit]


def _rank_repair_actions(
    *,
    summary: dict[str, Any],
    failure_row: dict[str, Any],
    decay_row: dict[str, Any],
) -> list[dict[str, Any]]:
    metrics = summary.get("recent_metrics", {}) if isinstance(summary.get("recent_metrics"), dict) else {}
    contribs = (
        summary.get("contribution_totals_recent", {})
        if isinstance(summary.get("contribution_totals_recent"), dict)
        else {}
    )
    total_return = _safe_float(metrics.get("total_return"))
    stock_drag = _stock_sum(contribs)
    crisis_plus_carry = _safe_float(contribs.get("crisis_contrib")) + _safe_float(
        contribs.get("carry_contrib")
    )
    worst = _worst_month(summary)
    avg_exposure = _safe_float(summary.get("avg_stock_exposure_recent"))
    worst_exposure = _safe_float(worst.get("avg_stock_exposure"))
    worst_risk_off = _safe_float(worst.get("risk_off_ratio"))
    failure_modes = failure_row.get("failure_modes", [])
    if not isinstance(failure_modes, list):
        failure_modes = []

    actions: list[dict[str, Any]] = []
    if "stock_alpha_sleeve_decay" in failure_modes or stock_drag < 0:
        actions.append(
            {
                "priority": 1,
                "action": "stock_sleeve_decay_root_cause",
                "why": "recent losses are primarily from stock alpha sleeves, not costs or paper evidence plumbing",
                "evidence": {
                    "recent_total_return": round(total_return, 6),
                    "stock_alpha_contribution": round(stock_drag, 6),
                    "dominant_sleeves": _dominant_decay_sleeves(decay_row),
                },
                "next_diagnostic": "attribute May-2026 selected names by factor bucket / industry / market-cap and compare against historical winning months",
                "do_not_do": "do not flip factor signs or lower exposure globally before cross-sectional attribution proves the regime boundary",
            }
        )
    if "regime_detection_lag_or_under_de_risking" in failure_modes or (
        worst_exposure > avg_exposure * 1.2 and worst_risk_off < 0.2
    ):
        actions.append(
            {
                "priority": 2,
                "action": "regime_trigger_timing_audit",
                "why": "worst month kept high stock exposure while risk-off was mostly inactive",
                "evidence": {
                    "worst_month": worst.get("month"),
                    "worst_month_return": _safe_float(worst.get("return")),
                    "worst_month_avg_stock_exposure": round(worst_exposure, 6),
                    "recent_avg_stock_exposure": round(avg_exposure, 6),
                    "worst_month_risk_off_ratio": round(worst_risk_off, 6),
                },
                "next_diagnostic": "trace risk-off inputs 20 trading days before 2026-05 drawdown and measure trigger delay vs loss start",
                "do_not_do": "do not simply tighten drawdown thresholds; that risks reacting after damage and overfitting May",
            }
        )
    if "defensive_sleeve_under_offset" in failure_modes:
        actions.append(
            {
                "priority": 3,
                "action": "defensive_offset_quality_audit",
                "why": "crisis/carry contribution was positive but too small to offset stock sleeve losses",
                "evidence": {
                    "stock_alpha_contribution": round(stock_drag, 6),
                    "crisis_plus_carry_contribution": round(crisis_plus_carry, 6),
                    "offset_ratio_vs_stock_drag": round(
                        abs(crisis_plus_carry) / abs(stock_drag), 6
                    )
                    if stock_drag
                    else 0.0,
                },
                "next_diagnostic": "test whether available defensive assets had positive returns in May-2026 and whether allocation arrived before or after the loss cluster",
                "do_not_do": "do not just increase crisis weight without proving the sleeve is a contemporaneous hedge",
            }
        )
    if "drawdown_scaling_active_after_damage" in failure_modes:
        actions.append(
            {
                "priority": 4,
                "action": "drawdown_scaling_lag_audit",
                "why": "drawdown scaling appears after losses, so it may protect capital but not prevent the initial regime loss",
                "evidence": {
                    "drawdown_scaling_ratio": failure_row.get("drawdown_scaling_ratio"),
                    "mdd_state_counts_recent": summary.get("mdd_state_counts_recent", {}),
                },
                "next_diagnostic": "compare dates of first stock-sleeve loss cluster, first reduce_scale day and subsequent recovery path",
                "do_not_do": "do not convert the MDD floor into a terminal stop; previous tests showed terminal stops are harmful",
            }
        )
    actions.sort(key=lambda row: int(row["priority"]))
    return actions


def build_report(
    regime: dict[str, Any],
    failure: dict[str, Any],
    decay: dict[str, Any],
    generated_at: datetime,
) -> dict[str, Any]:
    summaries = _as_dict_by_candidate(regime.get("summaries"))
    failures = _as_dict_by_candidate(failure.get("classifications"))
    decays = _as_dict_by_candidate(decay.get("candidates"))
    candidates: list[dict[str, Any]] = []
    for name, summary in summaries.items():
        failure_row = failures.get(name, {})
        decay_row = decays.get(name, {})
        metrics = summary.get("recent_metrics", {}) if isinstance(summary.get("recent_metrics"), dict) else {}
        contribs = (
            summary.get("contribution_totals_recent", {})
            if isinstance(summary.get("contribution_totals_recent"), dict)
            else {}
        )
        candidates.append(
            {
                "candidate": name,
                "recent_sharpe": _safe_float(metrics.get("sharpe_ratio")),
                "recent_total_return": _safe_float(metrics.get("total_return")),
                "recent_mdd": _safe_float(metrics.get("max_drawdown")),
                "stock_alpha_contribution": round(_stock_sum(contribs), 6),
                "crisis_contribution": round(_safe_float(contribs.get("crisis_contrib")), 6),
                "carry_contribution": round(_safe_float(contribs.get("carry_contrib")), 6),
                "worst_month": _worst_month(summary),
                "failure_modes": failure_row.get("failure_modes", []),
                "repair_actions": _rank_repair_actions(
                    summary=summary, failure_row=failure_row, decay_row=decay_row
                ),
            }
        )
    global_actions = sorted(
        {action["action"] for row in candidates for action in row["repair_actions"]}
    )
    return {
        "ts": generated_at.isoformat(),
        "version": "V29-recent90-repair-plan",
        "research_only": True,
        "production_ready": False,
        "not_parameter_tuning": True,
        "inputs": {
            "regime_version": regime.get("version"),
            "failure_version": failure.get("version"),
            "decay_version": decay.get("version"),
        },
        "candidate_count": len(candidates),
        "global_next_actions": global_actions,
        "candidates": candidates,
        "decision": {
            "can_claim_production_ready": False,
            "should_change_strategy_parameters_now": False,
            "next_step_class": "root-cause diagnostics for May-2026 stock sleeve decay and regime timing",
        },
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# V29 recent-90 修复计划（非调参）",
        "",
        "状态：研究诊断；不改变策略参数；不解除生产 blocker。",
        "",
        f"- candidate_count: {report['candidate_count']}",
        f"- global_next_actions: {', '.join(report['global_next_actions'])}",
        "- should_change_strategy_parameters_now: false",
        "",
    ]
    for candidate in report["candidates"]:
        lines.extend(
            [
                f"## {candidate['candidate']}",
                "",
                f"- recent_sharpe: {candidate['recent_sharpe']}",
                f"- recent_total_return: {candidate['recent_total_return']}",
                f"- recent_mdd: {candidate['recent_mdd']}",
                f"- stock_alpha_contribution: {candidate['stock_alpha_contribution']}",
                f"- crisis_contribution: {candidate['crisis_contribution']}",
                f"- worst_month: {candidate['worst_month'].get('month') if isinstance(candidate['worst_month'], dict) else None}",
                "",
                "### 下一步动作",
                "",
            ]
        )
        for action in candidate["repair_actions"]:
            lines.extend(
                [
                    f"{action['priority']}. **{action['action']}**",
                    f"   - why: {action['why']}",
                    f"   - next_diagnostic: {action['next_diagnostic']}",
                    f"   - do_not_do: {action['do_not_do']}",
                ]
            )
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = _parse_args()
    report = build_report(
        _load_json(Path(args.regime_json)),
        _load_json(Path(args.failure_json)),
        _load_json(Path(args.decay_json)),
        datetime.now(timezone.utc),
    )
    _write_json(Path(args.output_json), report)
    _write_markdown(Path(args.report_md), report)
    print(
        json.dumps(
            {
                "production_ready": False,
                "not_parameter_tuning": True,
                "candidate_count": report["candidate_count"],
                "global_next_actions": report["global_next_actions"],
                "output": args.output_json,
                "report": args.report_md,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
