#!/usr/bin/env python3
"""Prepare a fail-closed V40 paper-shadow launch package."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _paths import RESULTS_DIR

DEFAULT_RESEARCH = RESULTS_DIR / "quant_v40_stock_alpha_defensive_basket.json"
DEFAULT_ROBUSTNESS = RESULTS_DIR / "quant_v40_robustness.json"
DEFAULT_OUTPUT = RESULTS_DIR / "quant_v40_paper_shadow_package.json"
DEFAULT_RUNBOOK = Path("docs/research/quant_v40_paper_shadow_runbook.md")

EXTERNAL_PAPER_BLOCKERS = [
    "approved external WORM archive evidence",
    "approved Secret Manager evidence",
    "approval-service flow and approval reference",
    "real market-data provider entitlement",
    "exchange-backed position provider evidence",
    "broker order/fill log archive path",
    "90-day paper trading schedule and monitoring owner",
    "production calendar evidence",
    "capacity and disaster-recovery evidence",
]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research", default=str(DEFAULT_RESEARCH))
    parser.add_argument("--robustness", default=str(DEFAULT_ROBUSTNESS))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--runbook", default=str(DEFAULT_RUNBOOK))
    return parser.parse_args()


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"invalid JSON object: {path}")
    return data


def _find_candidate(research: dict[str, Any], candidate: dict[str, float]) -> dict[str, Any]:
    for row in research.get("results", []):
        scenario = row.get("scenario", {})
        if (
            abs(float(scenario.get("capital", -1)) - float(candidate["capital"])) <= 1e-9
            and abs(float(scenario.get("stock_alpha_cap", -1)) - float(candidate["stock_cap"]))
            <= 1e-12
            and abs(float(scenario.get("crisis_cap", -1)) - float(candidate["crisis_cap"]))
            <= 1e-12
            and int(scenario.get("stock_rebalance_freq", 40)) == 40
        ):
            return row
    raise RuntimeError("V40 paper-shadow candidate not found in research results")


def build_package(research_path: Path, robustness_path: Path) -> dict[str, Any]:
    research = _load_json(research_path)
    robustness = _load_json(robustness_path)
    candidate = robustness["candidate"]
    row = _find_candidate(research, candidate)
    robustness_blockers = list(robustness.get("blockers", []))
    shadow_candidate_exists = bool(
        row["gates"]["relative_improvement_gate_passes"]
        and robustness["gates"].get("candidate_default_relative_passes")
        and robustness["gates"].get("candidate_cost_stress_relative_passes")
    )
    paper_shadow_ready = bool(
        shadow_candidate_exists
        and robustness.get("robustness_gate_passes") is True
        and not robustness_blockers
    )
    blockers = [*robustness_blockers, *EXTERNAL_PAPER_BLOCKERS]
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V40-paper-shadow-package-v1",
        "research_only": True,
        "production_ready": False,
        "paper_shadow_ready": paper_shadow_ready,
        "shadow_candidate_exists": shadow_candidate_exists,
        "candidate": {
            "capital": float(candidate["capital"]),
            "stock_alpha_cap": float(candidate["stock_cap"]),
            "crisis_cap": float(candidate["crisis_cap"]),
            "stock_rebalance_freq": 40,
            "crisis_assets": ["518880", "511010", "511260"],
            "auto_trade_enabled": False,
            "live_order_submission_allowed": False,
            "max_live_capital_fraction": 0.0,
        },
        "candidate_metrics": {
            "default": row["full"],
            "robustness_default": robustness["candidate_metrics"]["default"],
            "robustness_cost_stress": robustness["candidate_metrics"]["cost_stress"],
        },
        "required_shadow_controls": [
            "read-only market data subscription",
            "paper account only, no broker live-order permission",
            "all proposed orders written to audit bus before simulated fill",
            "daily cost/slippage report",
            "weekly backtest-vs-paper drift report",
            "manual approval before any change to candidate parameters",
        ],
        "blockers": blockers,
        "source_files": {
            "research": str(research_path),
            "robustness": str(robustness_path),
        },
    }


def _write_runbook(path: Path, package: dict[str, Any]) -> None:
    candidate = package["candidate"]
    lines = [
        "# V40 Paper-Shadow 启动手册",
        "",
        f"生成时间：{package['ts']}",
        "",
        "## 状态",
        "",
        f"- `paper_shadow_ready={str(package['paper_shadow_ready']).lower()}`",
        "- `production_ready=false`",
        "- 该包只允许只读模拟盘影子验证，不允许实盘下单。",
        "",
        "## 固定候选",
        "",
        f"- 资金：{candidate['capital']:.0f} 元",
        f"- 股票预算上限：{candidate['stock_alpha_cap']:.2%}",
        f"- 危机预算上限：{candidate['crisis_cap']:.2%}",
        f"- 股票调仓周期：{candidate['stock_rebalance_freq']} 日",
        "- 危机资产：黄金 ETF 518880、国债 ETF 511010、十年国债 ETF 511260",
        "- `auto_trade_enabled=false`",
        "- `live_order_submission_allowed=false`",
        "- `max_live_capital_fraction=0.0`",
        "",
        "## 启动前阻塞项",
        "",
        *[f"- {item}" for item in package["blockers"]],
        "",
        "## 运行控制",
        "",
        *[f"- {item}" for item in package["required_shadow_controls"]],
        "",
        "## 判断",
        "",
        "该候选默认与高成本单点相对通过，但 V40 robustness 失败，不能进入生产。只有完成外部证据、90 天 paper shadow、滑点/成交偏差和周度回放对账后，才能重新评估是否进入更严格的 paper trading。",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    package = build_package(Path(args.research), Path(args.robustness))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_runbook(Path(args.runbook), package)
    print(
        json.dumps(
            {
                "production_ready": False,
                "paper_shadow_ready": package["paper_shadow_ready"],
                "shadow_candidate_exists": package["shadow_candidate_exists"],
                "blocker_count": len(package["blockers"]),
                "output": str(output),
                "runbook": str(Path(args.runbook)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
