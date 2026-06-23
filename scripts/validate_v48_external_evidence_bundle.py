#!/usr/bin/env python3
"""Validate a production external-evidence bundle before V44 ingestion."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

from _paths import PROJECT_DIR, RESULTS_DIR

sys.path.insert(0, str(PROJECT_DIR / "src"))

from quant_trading.deployment import PRODUCTION_EVIDENCE_REQUIREMENTS, evaluate_production_readiness

DEFAULT_TEMPLATE = PROJECT_DIR / "config" / "production_external_evidence_bundle.v48.template.json"
DEFAULT_OUTPUT = RESULTS_DIR / "quant_v48_external_evidence_bundle_gate.json"
DEFAULT_REPORT = Path("docs/research/quant_v48_external_evidence_bundle_gate.md")
DEFAULT_V44_REFS = RESULTS_DIR / "quant_v48_v44_external_evidence_refs.json"

SENSITIVE_KEY_RE = re.compile(r"(^|[_-])(token|password|api[_-]?key|private[_-]?key|secret)([_-]|$)", re.I)
SENSITIVE_VALUE_RE = re.compile(r"(Bearer\s+[A-Za-z0-9._=-]+|sk-[A-Za-z0-9]{12,}|AKIA[0-9A-Z]{16})")
SENSITIVE_KEY_ALLOWLIST = frozenset({"secret_manager"})


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-bundle-json", default="")
    parser.add_argument("--template-json", default=str(DEFAULT_TEMPLATE))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--report-md", default=str(DEFAULT_REPORT))
    parser.add_argument("--v44-evidence-refs-json", default=str(DEFAULT_V44_REFS))
    parser.add_argument("--write-template", action="store_true")
    parser.add_argument("--write-v44-evidence-refs", action="store_true")
    parser.add_argument("--require-bundle-valid", action="store_true")
    return parser.parse_args()


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
            if SENSITIVE_KEY_RE.search(key) and key not in SENSITIVE_KEY_ALLOWLIST:
                blockers.append(f"发现疑似敏感字段：{child_path}")
            blockers.extend(_scan_sensitive_material(value, child_path))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            blockers.extend(_scan_sensitive_material(value, f"{path}[{index}]"))
    elif isinstance(node, str) and SENSITIVE_VALUE_RE.search(node):
        blockers.append(f"发现疑似敏感值：{path}")
    return blockers


def _refs_section(bundle: dict[str, Any], key: str, blockers: list[str]) -> dict[str, str]:
    raw = bundle.get(key, {})
    if not isinstance(raw, dict):
        blockers.append(f"{key} 必须是对象")
        return {}
    return {str(item_key): str(value).strip() for item_key, value in raw.items()}


def _v30_module() -> ModuleType:
    return _load_script_module(
        "validate_v30_production_data_gate_v48",
        PROJECT_DIR / "scripts" / "validate_v30_production_data_gate.py",
    )


def _validate_production_data_refs(refs: dict[str, str]) -> tuple[bool, list[str], list[str]]:
    v30 = _v30_module()
    blockers: list[str] = []
    required_keys: list[str] = []
    for requirement in v30._evidence_requirements():
        required_keys.append(str(requirement.key))
        passes, blocker = v30._validate_evidence_ref(requirement, refs)
        if not passes and blocker is not None:
            blockers.append(blocker)
    return not blockers, blockers, required_keys


def build_template() -> dict[str, Any]:
    """Build a non-secret evidence-bundle template for the user to fill."""

    v30 = _v30_module()
    return {
        "version": "V48-production-external-evidence-bundle-template",
        "template_only": True,
        "production_ready": False,
        "说明": "仅填写外部证明 reference，不要填写 token、API key、密码或私钥。",
        "external_refs": {
            requirement.key: "" for requirement in PRODUCTION_EVIDENCE_REQUIREMENTS
        },
        "production_data_refs": {
            requirement.key: "" for requirement in v30._evidence_requirements()
        },
        "required_real_world_actions": [
            "向数据供应商或券商获取真实 PIT 股票池、退市/ST/停牌/涨跌停状态、复权因子复核、tick/minute 执行历史。",
            "向券商或模拟盘服务导出 90 天 paper trading 的订单、成交、持仓、滑点和审计归档 reference。",
            "在 WORM/Object Lock、Secret Manager、Approval Service、容量压测和 DR 演练系统中生成外部可验证 reference。",
            "只填写 reference，不填写任何密钥值或账户密码。",
        ],
    }


def validate_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    blockers = _scan_sensitive_material(bundle)
    external_refs = _refs_section(bundle, "external_refs", blockers)
    production_data_refs = _refs_section(bundle, "production_data_refs", blockers)

    external_report = evaluate_production_readiness(external_refs)
    production_data_passes, production_data_blockers, production_data_keys = (
        _validate_production_data_refs(production_data_refs)
    )
    external_blockers = list(external_report.blockers)
    blocker_groups = {
        "sensitive_material": blockers,
        "v44_external_refs": external_blockers,
        "v30_production_data_refs": production_data_blockers,
    }
    all_blockers = [
        *blocker_groups["sensitive_material"],
        *blocker_groups["v44_external_refs"],
        *blocker_groups["v30_production_data_refs"],
    ]
    bundle_valid = not all_blockers
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V48-production-external-evidence-bundle-gate",
        "production_ready": False,
        "evidence_bundle_valid": bundle_valid,
        "scope_status": {
            "sensitive_material_free": not blocker_groups["sensitive_material"],
            "v44_external_refs_pass": external_report.passed,
            "v30_production_data_refs_pass": production_data_passes,
        },
        "provided_external_ref_keys": sorted(key for key, value in external_refs.items() if value),
        "provided_production_data_ref_keys": sorted(
            key for key, value in production_data_refs.items() if value
        ),
        "required_external_ref_keys": [
            requirement.key for requirement in PRODUCTION_EVIDENCE_REQUIREMENTS
        ],
        "required_production_data_ref_keys": production_data_keys,
        "missing_external_ref_keys": [
            requirement.key
            for requirement in PRODUCTION_EVIDENCE_REQUIREMENTS
            if not external_refs.get(requirement.key)
        ],
        "missing_production_data_ref_keys": [
            key for key in production_data_keys if not production_data_refs.get(key)
        ],
        "blocker_groups": blocker_groups,
        "blockers": all_blockers,
        "v44_external_refs": external_refs,
        "production_data_refs": production_data_refs,
        "next_actions": [
            "把真实外部 refs 填入 V48 模板后重新运行本脚本。",
            "V48 通过后使用 --write-v44-evidence-refs 生成 V44 可读取的 refs JSON。",
            "继续运行 V30/V42/V44；V48 通过不代表生产准入通过。",
        ],
        "note": "V48 只校验外部证据包格式和可信 reference；不会生成真实 vendor/broker/WORM 证据，也不会开启实盘。",
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    status = payload["scope_status"]
    lines = [
        "# V48 外部生产证据包校验",
        "",
        "状态：证据包校验已生成，当前不代表生产批准。",
        "",
        "## 结论",
        "",
        f"- `evidence_bundle_valid={str(payload['evidence_bundle_valid']).lower()}`",
        "- `production_ready=false`",
        f"- 不含敏感材料：{str(status['sensitive_material_free']).lower()}",
        f"- V44 外部 refs 通过：{str(status['v44_external_refs_pass']).lower()}",
        f"- V30 生产数据 refs 通过：{str(status['v30_production_data_refs_pass']).lower()}",
        f"- 阻塞项数量：{len(payload['blockers'])}",
        "",
        "## 缺失的 V44 外部 refs",
        "",
    ]
    lines.extend(f"- `{key}`" for key in payload["missing_external_ref_keys"])
    if not payload["missing_external_ref_keys"]:
        lines.append("- 无")
    lines.extend(["", "## 缺失的 V30 生产数据 refs", ""])
    lines.extend(f"- `{key}`" for key in payload["missing_production_data_ref_keys"])
    if not payload["missing_production_data_ref_keys"]:
        lines.append("- 无")
    lines.extend(["", "## 阻塞项", ""])
    if payload["blockers"]:
        lines.extend(f"- {item}" for item in payload["blockers"])
    else:
        lines.append("- 无")
    lines.extend(
        [
            "",
            "## 哪些需要人工/外部系统提供",
            "",
            "- 真实 PIT 股票池、真实交易状态、真实 corporate action 复核、真实 tick/minute 数据和订单成交回放，需要数据供应商或券商提供。",
            "- WORM/Object Lock、Secret Manager、Approval Service、provider entitlement、broker position/borrow feed、容量压测和 DR 演练 reference，需要对应外部系统提供。",
            "- 90 天 paper trading 不能本地伪造，需要真实行情、模拟成交、每日审计归档和券商/交易所持仓快照持续产生。",
            "",
            "## 我已经能自动处理的部分",
            "",
            "- 生成无密钥模板。",
            "- 拒绝 token/API key/password/private key 等敏感字段和值。",
            "- 拒绝 mock/local/sample/pending/todo 等伪证据。",
            "- 校验 V44 和 V30 所需 reference 前缀。",
            "- 可在通过后生成 V44 可读取的外部 refs JSON。",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    template = build_template()
    template_path = Path(args.template_json)
    if args.write_template or not template_path.exists():
        _write_json(template_path, template)

    bundle = _load_json_object(Path(args.evidence_bundle_json)) if args.evidence_bundle_json else {}
    payload = validate_bundle(bundle)

    v44_refs_path: Path | None = None
    if args.write_v44_evidence_refs and payload["evidence_bundle_valid"]:
        v44_refs_path = Path(args.v44_evidence_refs_json)
        _write_json(v44_refs_path, payload["v44_external_refs"])
    payload["generated_files"] = {
        "template": str(template_path),
        "v44_evidence_refs": str(v44_refs_path) if v44_refs_path else "",
    }
    output = Path(args.output_json)
    _write_json(output, payload)
    _write_report(Path(args.report_md), payload)
    print(
        json.dumps(
            {
                "production_ready": False,
                "evidence_bundle_valid": payload["evidence_bundle_valid"],
                "blocker_count": len(payload["blockers"]),
                "output": str(output),
                "template": str(template_path),
                "v44_evidence_refs": str(v44_refs_path) if v44_refs_path else "",
                "report": str(Path(args.report_md)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.require_bundle_valid and not payload["evidence_bundle_valid"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
