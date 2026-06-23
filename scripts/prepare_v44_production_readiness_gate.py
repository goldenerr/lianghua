#!/usr/bin/env python3
"""Build the V44 consolidated production-readiness gate."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _paths import PROJECT_DIR, RESULTS_DIR

sys.path.insert(0, str(PROJECT_DIR / "src"))

from quant_trading.deployment import PRODUCTION_EVIDENCE_REQUIREMENTS, evaluate_production_readiness

DEFAULT_V40_PACKAGE = RESULTS_DIR / "quant_v40_paper_shadow_package.json"
DEFAULT_V42_GATE = RESULTS_DIR / "quant_v42_paper_shadow_evidence_gate.json"
DEFAULT_V43_GATE = RESULTS_DIR / "quant_v43_local_capacity_dr_gate.json"
DEFAULT_OUTPUT = RESULTS_DIR / "quant_v44_production_readiness_gate.json"
DEFAULT_REPORT = Path("docs/research/quant_v44_production_readiness_gate.md")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v40-package-json", default=str(DEFAULT_V40_PACKAGE))
    parser.add_argument("--v42-gate-json", default=str(DEFAULT_V42_GATE))
    parser.add_argument("--v43-gate-json", default=str(DEFAULT_V43_GATE))
    parser.add_argument(
        "--evidence-file",
        default=os.getenv("QUANT_PRODUCTION_EVIDENCE_FILE", ""),
        help="Optional JSON object with external production evidence refs.",
    )
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--report-md", default=str(DEFAULT_REPORT))
    parser.add_argument("--require-production-ready", action="store_true")
    return parser.parse_args()


def _load_json_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"JSON must be an object: {path}")
    return data


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


def _candidate_blockers(v40_package: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if v40_package.get("shadow_candidate_exists") is not True:
        blockers.append("V40 影子模拟盘候选不存在")
    if v40_package.get("paper_shadow_ready") is not True:
        blockers.append("V40 影子模拟盘包未就绪")
    package_blockers = list(v40_package.get("blockers", []))
    if package_blockers:
        blockers.append(f"V40 影子模拟盘包仍有 {len(package_blockers)} 个阻塞项")
    candidate = v40_package.get("candidate", {})
    if not isinstance(candidate, dict):
        blockers.append("V40 包缺少候选配置")
    else:
        if bool(candidate.get("auto_trade_enabled")):
            blockers.append("V40 候选 auto_trade_enabled 必须为 false")
        if bool(candidate.get("live_order_submission_allowed")):
            blockers.append("V40 候选 live_order_submission_allowed 必须为 false")
        if float(candidate.get("max_live_capital_fraction", 1.0)) != 0.0:
            blockers.append("V40 候选 max_live_capital_fraction 必须为 0.0")
    return blockers


def _paper_blockers(v42_gate: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if v42_gate.get("paper_evidence_ready") is not True:
        blockers.append("V42 影子模拟盘 90 天证据门禁未通过")
    if v42_gate.get("production_ready") is True:
        blockers.append("V42 门禁不允许直接声明 production_ready=true")
    for item in v42_gate.get("blockers", []):
        blockers.append(f"V42: {item}")
    return blockers


def _capacity_blockers(v43_gate: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if v43_gate.get("local_capacity_dr_gate_passes") is not True:
        blockers.append("V43 本地容量/容灾门禁未通过")
    if v43_gate.get("production_ready") is True:
        blockers.append("V43 本地门禁不允许直接声明 production_ready=true")
    for item in v43_gate.get("local_blockers", []):
        blockers.append(f"V43 本地门禁: {item}")
    return blockers


def _scope_status(
    *,
    candidate_blockers: list[str],
    paper_blockers: list[str],
    capacity_blockers: list[str],
    external_blockers: list[str],
) -> dict[str, bool]:
    return {
        "candidate_package_passes": not candidate_blockers,
        "paper_evidence_passes": not paper_blockers,
        "local_capacity_dr_passes": not capacity_blockers,
        "external_evidence_passes": not external_blockers,
    }


def build_readiness_gate(
    *,
    v40_package: dict[str, Any],
    v42_gate: dict[str, Any],
    v43_gate: dict[str, Any],
    evidence: dict[str, str],
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or datetime.now(timezone.utc)
    external_report = evaluate_production_readiness(evidence)
    candidate_blockers = _candidate_blockers(v40_package)
    paper_blockers = _paper_blockers(v42_gate)
    capacity_blockers = _capacity_blockers(v43_gate)
    external_blockers = list(external_report.blockers)
    scope_status = _scope_status(
        candidate_blockers=candidate_blockers,
        paper_blockers=paper_blockers,
        capacity_blockers=capacity_blockers,
        external_blockers=external_blockers,
    )
    production_blockers = [
        *candidate_blockers,
        *paper_blockers,
        *capacity_blockers,
        *external_blockers,
    ]
    production_ready = not production_blockers
    return {
        "ts": generated_at.isoformat(),
        "version": "V44-consolidated-production-readiness-gate",
        "research_only": False,
        "production_ready": production_ready,
        "scope_status": scope_status,
        "candidate": v40_package.get("candidate", {}),
        "provided_evidence_keys": sorted(key for key, value in evidence.items() if value),
        "required_external_evidence_keys": [
            requirement.key for requirement in PRODUCTION_EVIDENCE_REQUIREMENTS
        ],
        "missing_external_evidence_keys": [
            requirement.key for requirement in PRODUCTION_EVIDENCE_REQUIREMENTS if not evidence.get(requirement.key)
        ],
        "candidate_blockers": candidate_blockers,
        "paper_evidence_blockers": paper_blockers,
        "capacity_dr_blockers": capacity_blockers,
        "external_evidence_blockers": external_blockers,
        "production_blockers": production_blockers,
        "source_gate_summary": {
            "v40_shadow_candidate_exists": bool(v40_package.get("shadow_candidate_exists")),
            "v40_paper_shadow_ready": bool(v40_package.get("paper_shadow_ready")),
            "v42_paper_evidence_ready": bool(v42_gate.get("paper_evidence_ready")),
            "v43_local_capacity_dr_gate_passes": bool(
                v43_gate.get("local_capacity_dr_gate_passes")
            ),
            "v43_production_like": bool(v43_gate.get("production_like")),
        },
        "next_actions": [
            "修复 V40 robustness 与候选包 blocker。",
            "接入真实只读行情、模拟成交、每日审计归档和券商/交易所持仓快照，连续生成至少 90 天 V42 日报。",
            "提供外部 WORM、Secret Manager、Approval Service、真实 provider、position provider、broker logs、生产日历、签名插件、容量压测和 DR 演练 refs。",
            "所有外部 refs 到位后重新运行本脚本，并继续保持实盘下单关闭，直到风控审批和生产 gate 全部通过。",
        ],
        "note": "V44 是总门禁，任何子门禁或外部证据缺失都会保持 production_ready=false。",
    }


def _zh_blocker(item: str) -> str:
    replacements = {
        "missing external immutable WORM archive attestation": "缺少外部不可变 WORM 归档证明",
        "missing approved production secret-manager resolver evidence": "缺少已批准的生产 Secret Manager 证明",
        "missing risk/configuration approval service evidence": "缺少风控/配置审批服务证明",
        "missing approved non-mock market-data provider evidence": "缺少已批准的非 mock 行情供应商证明",
        "missing data-provider entitlement, license and redistribution evidence": "缺少数据供应商授权、许可和再分发证明",
        "missing archive-complete alternative-data readiness artifact": "缺少另类数据归档完整性证明",
        "missing exchange-backed reconciled position provider evidence": "缺少交易所/券商背书的对账持仓服务证明",
        "missing broker-backed borrow availability feed and account binding evidence": "缺少券商背书的借券可用性和账户绑定证明",
        "missing three-month paper-trading gate evidence using real market data": "缺少使用真实行情完成的三个月模拟盘证明",
        "missing signed plugin load and code-review evidence": "缺少签名插件加载与代码审查证明",
        "missing approved production exchange calendar evidence": "缺少已批准的生产交易所日历证明",
        "missing approved futures rollover execution runbook/evidence": "缺少已批准的期货换月执行手册/证明",
        "missing production-like capacity benchmark report": "缺少类生产环境容量压测报告",
        "missing multi-region failover and DR test report": "缺少多区域故障切换和灾备演练报告",
        "placeholder/local evidence is not acceptable": "占位/本地证据不可接受",
        "evidence must use one of": "证据引用前缀必须是以下之一",
        "paper-shadow package still has": "影子模拟盘包仍有",
        " blocker(s)": " 个阻塞项",
        "missing daily paper-shadow reports": "缺少每日影子模拟盘报告",
        "missing trusted paper_trading_90d evidence ref": "缺少可信 paper_trading_90d 证据引用",
    }
    text = item
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def _markdown_list(items: list[str]) -> str:
    if not items:
        return "- 无\n"
    return "".join(f"- {_zh_blocker(str(item))}\n" for item in items)


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    status = payload["scope_status"]
    lines = [
        "# V44 生产准入总门禁",
        "",
        "状态：总门禁已生成，当前不允许进入生产。",
        "",
        "## 结论",
        "",
        f"- `production_ready={str(payload['production_ready']).lower()}`",
        f"- 候选包通过：{str(status['candidate_package_passes']).lower()}",
        f"- 90 天影子模拟盘证据通过：{str(status['paper_evidence_passes']).lower()}",
        f"- 本地容量/容灾门禁通过：{str(status['local_capacity_dr_passes']).lower()}",
        f"- 外部生产证据通过：{str(status['external_evidence_passes']).lower()}",
        f"- 总阻塞项数量：{len(payload['production_blockers'])}",
        "",
        "## 候选配置",
        "",
    ]
    candidate = payload.get("candidate", {})
    if isinstance(candidate, dict) and candidate:
        for key in (
            "capital",
            "stock_alpha_cap",
            "crisis_cap",
            "stock_rebalance_freq",
            "auto_trade_enabled",
            "live_order_submission_allowed",
            "max_live_capital_fraction",
        ):
            lines.append(f"- `{key}`：{candidate.get(key)}")
    else:
        lines.append("- 无")
    lines.extend(
        [
            "",
            "## 候选包阻塞项",
            "",
            _markdown_list(payload["candidate_blockers"]),
            "## 90 天影子模拟盘阻塞项",
            "",
            _markdown_list(payload["paper_evidence_blockers"]),
            "## 容量与容灾阻塞项",
            "",
            _markdown_list(payload["capacity_dr_blockers"]),
            "## 外部生产证据阻塞项",
            "",
            _markdown_list(payload["external_evidence_blockers"]),
            "## 下一步",
            "",
            _markdown_list(payload["next_actions"]),
            "## 说明",
            "",
            "- V44 只是总门禁，不会启动实盘交易。",
            "- 外部证据齐全只是必要条件；策略 robustness、90 天模拟盘、容量/容灾和审批仍必须全部通过。",
            "- 不得把本地报告、mock、pending 或 todo 引用填入生产证据。",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    v40_package = _load_json_object(Path(args.v40_package_json))
    v42_gate = _load_json_object(Path(args.v42_gate_json))
    v43_gate = _load_json_object(Path(args.v43_gate_json))
    evidence = _load_evidence(str(args.evidence_file))
    payload = build_readiness_gate(
        v40_package=v40_package,
        v42_gate=v42_gate,
        v43_gate=v43_gate,
        evidence=evidence,
    )
    output = Path(args.output_json)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(Path(args.report_md), payload)
    print(
        json.dumps(
            {
                "production_ready": payload["production_ready"],
                "blocker_count": len(payload["production_blockers"]),
                "scope_status": payload["scope_status"],
                "output": str(output),
                "report": str(Path(args.report_md)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.require_production_ready and not payload["production_ready"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
