#!/usr/bin/env python3
"""Prepare the V29 approved-paper-trading launch package.

This script is deliberately a gate and packaging tool, not a production
approval shortcut. It checks the V29 research scorecard, robustness evidence and
external evidence references, then emits a machine-readable launch package plus
an operator runbook for the real 90-day paper-trading stage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _paths import PROJECT_DIR, RESULTS_DIR

sys.path.insert(0, str(PROJECT_DIR / "src"))

from quant_trading.deployment import (
    PRODUCTION_EVIDENCE_REQUIREMENTS,
    evaluate_production_readiness,
)
from quant_trading.strategy.v29_spec import V29StrategySpec

DEFAULT_READINESS_JSON = RESULTS_DIR / "quant_v27_strategy_readiness.json"
DEFAULT_V29_RESEARCH_JSON = (
    RESULTS_DIR / "quant_logic_research_v29_portfolio_layer_20y_v31_execution_constrained.json"
)
DEFAULT_V29_ROBUSTNESS_JSON = RESULTS_DIR / "quant_v29_robustness_20y_v31_execution_constrained.json"
DEFAULT_EXTERNAL_GATE_JSON = RESULTS_DIR / "quant_external_evidence_gate_v26.json"
DEFAULT_STRATEGY_SPEC_YAML = (
    PROJECT_DIR / "config" / "strategies" / "v29_price_meta_longhorizon_guard.paper.yaml"
)
DEFAULT_OUTPUT_JSON = RESULTS_DIR / "quant_v29_paper_trading_launch_package.json"
DEFAULT_RUNBOOK = PROJECT_DIR / "docs" / "research" / "quant_v29_paper_trading_launch_runbook.md"

PAPER_START_REQUIRED_KEYS: tuple[str, ...] = (
    "external_worm_archive",
    "secret_manager",
    "approval_service",
    "real_market_data_provider",
    "provider_entitlement",
    "exchange_position_provider",
    "signed_plugin_review",
    "production_calendar",
    "capacity_benchmark",
)

FULL_PRODUCTION_REQUIRED_KEYS: tuple[str, ...] = tuple(
    requirement.key for requirement in PRODUCTION_EVIDENCE_REQUIREMENTS
)

V29_PRICE_ONLY_NOT_USED_KEYS: tuple[str, ...] = (
    "alt_archive_readiness",
    "broker_borrow_availability",
    "rollover_runbook",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readiness-json", default=str(DEFAULT_READINESS_JSON))
    parser.add_argument("--v29-research-json", default=str(DEFAULT_V29_RESEARCH_JSON))
    parser.add_argument("--v29-robustness-json", default=str(DEFAULT_V29_ROBUSTNESS_JSON))
    parser.add_argument("--external-gate-json", default=str(DEFAULT_EXTERNAL_GATE_JSON))
    parser.add_argument("--strategy-spec-yaml", default=str(DEFAULT_STRATEGY_SPEC_YAML))
    parser.add_argument(
        "--evidence-file",
        default=os.getenv("QUANT_PRODUCTION_EVIDENCE_FILE", ""),
        help="Optional JSON object with external evidence refs. Overrides refs in gate JSON.",
    )
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--runbook-md", default=str(DEFAULT_RUNBOOK))
    parser.add_argument(
        "--require-paper-launch-ready",
        action="store_true",
        help="Exit non-zero unless V29 can start approved paper trading.",
    )
    parser.add_argument(
        "--require-production-release-ready",
        action="store_true",
        help="Exit non-zero unless full production evidence is ready.",
    )
    return parser.parse_args()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve(path_value: str, default_dir: Path) -> Path:
    path = Path(path_value)
    if path.is_absolute() or path.parent != Path("."):
        return path
    return default_dir / path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_evidence(external_gate: dict[str, Any], evidence_file: str) -> dict[str, str]:
    raw_env = os.getenv("QUANT_PRODUCTION_EVIDENCE_JSON", "").strip()
    if raw_env:
        loaded = json.loads(raw_env)
    elif evidence_file:
        loaded = json.loads(Path(evidence_file).read_text(encoding="utf-8"))
    else:
        loaded = external_gate.get("evidence", {})
    if not isinstance(loaded, dict):
        raise RuntimeError("production evidence must be a JSON object")
    return {str(key): str(value) for key, value in loaded.items()}


def _validate_subset(evidence: dict[str, str], keys: tuple[str, ...]) -> list[str]:
    requirements = {requirement.key: requirement for requirement in PRODUCTION_EVIDENCE_REQUIREMENTS}
    blockers: list[str] = []
    for key in keys:
        requirement = requirements[key]
        blocker = requirement.validate(evidence)
        if blocker:
            blockers.append(blocker)
    return blockers


def _candidate_name(readiness: dict[str, Any]) -> str:
    candidate = readiness.get("recommended_candidate")
    if not isinstance(candidate, dict):
        candidate = readiness.get("best_candidate")
    if not isinstance(candidate, dict):
        return ""
    return str(candidate.get("name") or "")


def _candidate_metrics(readiness: dict[str, Any]) -> dict[str, Any]:
    candidate = readiness.get("recommended_candidate")
    if not isinstance(candidate, dict):
        candidate = readiness.get("best_candidate")
    if not isinstance(candidate, dict):
        return {}
    fields = (
        "name",
        "score",
        "full_sharpe",
        "avg_oos_sharpe",
        "min_oos_sharpe",
        "sharpe_decay",
        "max_drawdown",
        "win_rate",
        "annual_return",
        "annual_volatility",
        "wf_folds",
        "production_minimum_passes",
        "industry_leading_passes",
    )
    return {field: candidate.get(field) for field in fields}


def _strategy_blockers(
    readiness: dict[str, Any],
    robustness: dict[str, Any],
    research: dict[str, Any],
    strategy_spec: V29StrategySpec | None = None,
) -> list[str]:
    blockers: list[str] = []
    candidate = _candidate_name(readiness)
    if not candidate:
        blockers.append("missing recommended V29 candidate in readiness scorecard")
    if not candidate.startswith("v29_price_meta_") or "alt" in candidate.lower():
        blockers.append(f"recommended candidate is not the V29 price-only production path: {candidate}")
    if not bool(readiness.get("strategy_gate_passes")):
        blockers.append("strategy readiness gate is not passed")
    if not bool(readiness.get("industry_leading_candidate_exists")):
        blockers.append("industry-leading candidate gate is not passed")
    if not bool(robustness.get("robustness_gate_passes")):
        blockers.append("V29 robustness gate is not passed")
    robust_candidate = str(robustness.get("candidate") or "")
    if robust_candidate and candidate and robust_candidate != candidate:
        blockers.append(
            "readiness recommended candidate does not match robustness candidate: "
            f"{candidate} != {robust_candidate}"
        )
    if strategy_spec is not None and candidate and strategy_spec.candidate != candidate:
        blockers.append(
            "strategy spec candidate does not match readiness recommended candidate: "
            f"{strategy_spec.candidate} != {candidate}"
        )
    research_results = research.get("results", []) if isinstance(research, dict) else []
    result_names = {
        str(item.get("name"))
        for item in research_results
        if isinstance(item, dict) and item.get("name") is not None
    }
    if candidate and result_names and candidate not in result_names:
        blockers.append(f"recommended candidate is absent from V29 research results: {candidate}")
    return blockers


def build_launch_package(
    *,
    readiness: dict[str, Any],
    research: dict[str, Any],
    robustness: dict[str, Any],
    external_gate: dict[str, Any],
    evidence: dict[str, str],
    artifact_hashes: dict[str, str],
    strategy_spec: V29StrategySpec | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the fail-closed launch package payload."""

    generated_at = generated_at or datetime.now(timezone.utc)
    strategy_blockers = _strategy_blockers(readiness, robustness, research, strategy_spec)
    paper_blockers = _validate_subset(evidence, PAPER_START_REQUIRED_KEYS)
    full_report = evaluate_production_readiness(evidence)
    paper_launch_ready = not strategy_blockers and not paper_blockers
    production_release_ready = not strategy_blockers and full_report.passed
    missing_for_paper = [key for key in PAPER_START_REQUIRED_KEYS if not evidence.get(key, "").strip()]
    missing_for_full = [key for key in FULL_PRODUCTION_REQUIRED_KEYS if not evidence.get(key, "").strip()]

    return {
        "ts": generated_at.isoformat(),
        "version": "V29-paper-trading-launch-package",
        "research_only": False,
        "production_ready": production_release_ready,
        "paper_launch_ready": paper_launch_ready,
        "production_release_evidence_ready": full_report.passed,
        "candidate": _candidate_name(readiness),
        "candidate_metrics": _candidate_metrics(readiness),
        "candidate_scope": {
            "market": "A股",
            "style": "long_only_price_only_dynamic_sleeves",
            "uses_archived_snapshot_alt_data": False,
            "uses_short_or_borrow": False,
            "uses_futures_rollover": False,
            "carry_floor_asset": "511260 ETF history from approved market-data provider",
            "not_used_external_keys_for_candidate_scope": list(V29_PRICE_ONLY_NOT_USED_KEYS),
        },
        "strategy_spec": (
            {
                "strategy_id": strategy_spec.strategy_id,
                "candidate": strategy_spec.candidate,
                "stage": strategy_spec.stage,
                "production_ready": strategy_spec.production_ready,
                "paper_min_calendar_days": strategy_spec.controls.paper_min_calendar_days,
            }
            if strategy_spec is not None
            else {}
        ),
        "paper_start_required_keys": list(PAPER_START_REQUIRED_KEYS),
        "full_production_required_keys": list(FULL_PRODUCTION_REQUIRED_KEYS),
        "provided_evidence_keys": sorted(key for key, value in evidence.items() if value.strip()),
        "missing_evidence_for_paper_start": missing_for_paper,
        "missing_evidence_for_full_production": missing_for_full,
        "strategy_blockers": strategy_blockers,
        "paper_launch_blockers": paper_blockers,
        "production_release_blockers": list(strategy_blockers) + list(full_report.blockers),
        "launch_mode": "approved_90d_paper_trading" if paper_launch_ready else "blocked",
        "launch_controls": {
            "auto_trade_enabled": False,
            "live_order_submission_allowed": False,
            "max_live_capital_fraction": 0.0,
            "paper_min_calendar_days": 90,
            "paper_min_performance_vs_backtest": 0.70,
            "paper_required_slippage_model": "dynamic_slippage_and_fee_model",
            "fail_closed_on_missing_evidence": True,
            "no_secret_values_in_package": True,
        },
        "operator_env_refs": {
            "QUANT_PRODUCTION_EVIDENCE_FILE": "外部证据引用 JSON 的路径",
            "QUANT_SECRET_MANAGER_URL": "Secret Manager 服务端点，不是 token 内容",
            "QUANT_SECRET_MANAGER_TOKEN_REF": "Secret Manager token 引用，绝不能填写 token 值",
            "QUANT_APPROVAL_SERVICE_URL": "风控审批服务端点",
            "QUANT_POSITION_PROVIDER_URL": "交易所/券商对账持仓服务端点",
            "QUANT_MARKET_DATA_PROVIDER_REF": "已批准的数据供应商授权引用",
        },
        "artifact_hashes": artifact_hashes,
        "external_gate_snapshot": {
            "source_production_ready": bool(external_gate.get("production_ready")),
            "source_required_keys": external_gate.get("required_keys", []),
            "source_blockers": external_gate.get("missing_or_untrusted_blockers", []),
        },
        "next_actions": [
            "从已批准的 WORM、Secret Manager、审批服务、供应商授权和券商服务中填写外部证据引用。",
            "启动 90 天模拟盘服务前，必须使用 --require-paper-launch-ready 重新运行本脚本。",
            "模拟盘服务必须使用真实行情、动态成本和审计归档，连续运行至少 90 天。",
            "只有模拟盘证据存在后，才允许重新运行完整生产证据门禁和部署门禁。",
        ],
    }


def _markdown_list(items: list[str]) -> str:
    if not items:
        return "- 无\n"
    return "".join(f"- {_runbook_text(item)}\n" for item in items)


def _runbook_text(value: Any) -> str:
    text = str(value)
    replacements = {
        "missing external immutable WORM archive attestation": "缺少外部不可变 WORM 归档证明",
        "missing approved production secret-manager resolver evidence": "缺少已批准的生产 Secret Manager 解析器证明",
        "missing risk/configuration approval service evidence": "缺少风控/配置审批服务证明",
        "missing approved non-mock market-data provider evidence": "缺少已批准的非 mock 行情供应商证明",
        "missing data-provider entitlement, license and redistribution evidence": "缺少数据供应商授权、许可和再分发证明",
        "missing archive-complete alternative-data readiness artifact": "缺少另类数据归档完整性 artifact 证明",
        "missing exchange-backed reconciled position provider evidence": "缺少交易所/券商背书的对账持仓服务证明",
        "missing broker-backed borrow availability feed and account binding evidence": "缺少券商背书的借券可用性数据源和账户绑定证明",
        "missing three-month paper-trading gate evidence using real market data": "缺少使用真实行情完成的三个月模拟盘门禁证明",
        "missing signed plugin load and code-review evidence": "缺少签名插件加载和代码审查证明",
        "missing approved production exchange calendar evidence": "缺少已批准的生产交易所日历证明",
        "missing approved futures rollover execution runbook/evidence": "缺少已批准的期货换月执行操作手册/证明",
        "missing production-like capacity benchmark report": "缺少类生产环境容量压测报告",
        "missing multi-region failover and DR test report": "缺少多区域故障切换和灾备演练报告",
        "placeholder/local evidence is not acceptable": "占位/本地证据不可接受",
        "evidence must use one of": "证据引用前缀必须是以下之一",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def render_runbook(package: dict[str, Any]) -> str:
    """生成不泄露密钥材料的中文操作 runbook。"""

    metrics = package.get("candidate_metrics", {})
    lines = [
        "# V29 模拟盘启动运行手册",
        "",
        "状态：由启动包生成，不代表实盘交易批准。",
        "",
        "## 候选策略",
        "",
        f"- 候选：`{package.get('candidate')}`",
        f"- 模拟盘启动就绪：`{package.get('paper_launch_ready')}`",
        f"- 生产就绪：`{package.get('production_ready')}`",
        f"- 全样本 Sharpe：`{metrics.get('full_sharpe')}`",
        f"- OOS Sharpe: `{metrics.get('avg_oos_sharpe')}`",
        f"- 最低 OOS Sharpe：`{metrics.get('min_oos_sharpe')}`",
        f"- 最大回撤：`{metrics.get('max_drawdown')}`",
        "",
        "## 模拟盘启动前必须具备的证据",
        "",
        _markdown_list(package.get("paper_start_required_keys", [])),
        "## 当前模拟盘启动阻塞项",
        "",
        _markdown_list(package.get("paper_launch_blockers", [])),
        "## 当前完整生产阻塞项",
        "",
        _markdown_list(package.get("production_release_blockers", [])),
        "## 操作环境引用",
        "",
    ]
    for key, description in package.get("operator_env_refs", {}).items():
        lines.append(f"- `{key}`：{description}")
    lines.extend(
        [
            "",
            "## 控制要求",
            "",
            "- 90 天模拟盘阶段不得开启实盘下单。",
            "- 不得在本运行手册或证据 JSON 中放入 token、API Key 或 passphrase。",
            "- 任何缺失或占位证据引用都必须视为硬阻塞项。",
            "- 数据或代码发生实质变化后，必须重新运行策略 readiness 和鲁棒性 gate。",
            "",
            "## 下一步",
            "",
            _markdown_list(package.get("next_actions", [])),
        ]
    )
    return "\n".join(lines)


def main() -> None:
    args = _parse_args()
    readiness_path = _resolve(args.readiness_json, RESULTS_DIR)
    research_path = _resolve(args.v29_research_json, RESULTS_DIR)
    robustness_path = _resolve(args.v29_robustness_json, RESULTS_DIR)
    external_gate_path = _resolve(args.external_gate_json, RESULTS_DIR)
    strategy_spec_path = _resolve(args.strategy_spec_yaml, PROJECT_DIR)
    output_json_path = _resolve(args.output_json, RESULTS_DIR)
    runbook_path = _resolve(args.runbook_md, PROJECT_DIR)

    readiness = _read_json(readiness_path)
    research = _read_json(research_path)
    robustness = _read_json(robustness_path)
    external_gate = _read_json(external_gate_path)
    strategy_spec = V29StrategySpec.from_yaml(strategy_spec_path)
    evidence = _load_evidence(external_gate, args.evidence_file)
    artifact_hashes = {
        "readiness_json_sha256": _sha256_file(readiness_path),
        "v29_research_json_sha256": _sha256_file(research_path),
        "v29_robustness_json_sha256": _sha256_file(robustness_path),
        "external_gate_json_sha256": _sha256_file(external_gate_path),
        "strategy_spec_yaml_sha256": _sha256_file(strategy_spec_path),
    }
    package = build_launch_package(
        readiness=readiness,
        research=research,
        robustness=robustness,
        external_gate=external_gate,
        evidence=evidence,
        artifact_hashes=artifact_hashes,
        strategy_spec=strategy_spec,
    )
    output_json_path.parent.mkdir(parents=True, exist_ok=True)
    output_json_path.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
    runbook_path.parent.mkdir(parents=True, exist_ok=True)
    runbook_path.write_text(render_runbook(package), encoding="utf-8")

    print(
        "V29 paper launch package: "
        f"paper_launch_ready={package['paper_launch_ready']} "
        f"production_ready={package['production_ready']} "
        f"paper_blockers={len(package['paper_launch_blockers'])} "
        f"production_blockers={len(package['production_release_blockers'])}",
        flush=True,
    )
    print(f"Wrote {output_json_path}", flush=True)
    print(f"Wrote {runbook_path}", flush=True)
    if args.require_paper_launch_ready and not package["paper_launch_ready"]:
        raise SystemExit(1)
    if args.require_production_release_ready and not package["production_ready"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
