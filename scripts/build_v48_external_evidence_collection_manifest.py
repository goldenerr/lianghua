#!/usr/bin/env python3
"""Build the V48 external evidence collection manifest.

This is a collection-control artifact, not evidence. It converts the current V48
and V30 blocker set into a machine-readable checklist with accepted reference
prefixes, owner class, and collection sequence. It never marks production ready
and never substitutes local/free-source artifacts for vendor/broker evidence.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

from _paths import PROJECT_DIR, RESULTS_DIR

sys.path.insert(0, str(PROJECT_DIR / "src"))

from quant_trading.deployment import PRODUCTION_EVIDENCE_REQUIREMENTS

DEFAULT_V48_GATE = RESULTS_DIR / "quant_v48_external_evidence_bundle_gate.json"
DEFAULT_PAPER_HEALTH = RESULTS_DIR / "quant_v49_v45_paper_evidence_health.json"
DEFAULT_OUTPUT = RESULTS_DIR / "quant_v48_external_evidence_collection_manifest.json"
DEFAULT_REPORT = Path("docs/research/quant_v48_external_evidence_collection_manifest.md")

V44_OWNER = {
    "external_worm_archive": "platform/secops",
    "secret_manager": "platform/secops",
    "approval_service": "risk/compliance",
    "real_market_data_provider": "data/vendor-management",
    "provider_entitlement": "legal/data-vendor-management",
    "alt_archive_readiness": "data/platform",
    "exchange_position_provider": "broker-integration/ops",
    "broker_borrow_availability": "broker-integration/ops",
    "paper_trading_90d": "research/ops",
    "signed_plugin_review": "security/code-review",
    "production_calendar": "data/platform",
    "rollover_runbook": "trading-ops/risk",
    "capacity_benchmark": "sre/platform",
    "failover_dr_test": "sre/platform",
}

V30_OWNER = {
    "pit_security_master": "data/vendor-management",
    "trading_status_actions": "data/vendor-management",
    "corporate_action_reconciliation": "data/vendor-management/research",
    "real_tick_minute_execution_feed": "data/vendor-management/execution",
    "exchange_order_fill_replay": "broker-integration/execution",
}

PRIORITY = {
    "pit_security_master": 1,
    "trading_status_actions": 2,
    "corporate_action_reconciliation": 3,
    "real_tick_minute_execution_feed": 4,
    "exchange_order_fill_replay": 5,
    "provider_entitlement": 6,
    "real_market_data_provider": 7,
    "external_worm_archive": 8,
    "secret_manager": 9,
    "approval_service": 10,
    "exchange_position_provider": 11,
    "broker_borrow_availability": 12,
    "paper_trading_90d": 13,
    "production_calendar": 14,
    "capacity_benchmark": 15,
    "failover_dr_test": 16,
    "signed_plugin_review": 17,
    "alt_archive_readiness": 18,
    "rollover_runbook": 19,
}

COLLECTION_ACTIONS = {
    "pit_security_master": "Obtain vendor/broker PIT security-master extract covering listed/delisted/tradable state for the requested universe and attach immutable ref.",
    "trading_status_actions": "Obtain trading-day ST/suspension/limit-up/limit-down/tradability history with vendor/exchange provenance.",
    "corporate_action_reconciliation": "Reconcile local adjusted prices against vendor corporate-action/adjust-factor feed and archive the reconciliation artifact.",
    "real_tick_minute_execution_feed": "Procure authorized tick/minute/order-book execution-history feed for capacity and slippage validation.",
    "exchange_order_fill_replay": "Export broker/exchange-backed historical order/fill replay or certified simulator fill logs.",
    "external_worm_archive": "Provision external WORM/Object-Lock or vault audit archive and record attestation reference.",
    "secret_manager": "Bind production secrets to an approved secret manager; store only secret-manager reference, never secret values.",
    "approval_service": "Create risk/config approval workflow record and store approval reference.",
    "real_market_data_provider": "Bind real non-mock market-data provider contract/feed reference.",
    "provider_entitlement": "Attach license/entitlement/redistribution proof for every market-data source used by research and paper evidence.",
    "alt_archive_readiness": "Archive alternative-data readiness artifact if alt data remains in scope; otherwise attach approved out-of-scope decision.",
    "exchange_position_provider": "Bind exchange/broker position snapshot provider and reconcile account binding.",
    "broker_borrow_availability": "Bind broker-backed borrow/availability feed or approved long-only no-borrow scope decision.",
    "paper_trading_90d": "Let V49/V45 daily report cron accumulate natural 90-calendar/60-report-day evidence; do not backfill.",
    "signed_plugin_review": "Attach signed code-review/plugin-load artifact for production plugins.",
    "production_calendar": "Attach approved production exchange calendar evidence used by runtime and backtest alignment.",
    "rollover_runbook": "Attach futures rollover runbook/evidence or approved out-of-scope decision if futures are disabled.",
    "capacity_benchmark": "Run production-like capacity benchmark and archive report reference.",
    "failover_dr_test": "Run multi-region failover/DR exercise and archive signed report reference.",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v48-gate-json", default=str(DEFAULT_V48_GATE))
    parser.add_argument("--paper-health-json", default=str(DEFAULT_PAPER_HEALTH))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--report-md", default=str(DEFAULT_REPORT))
    return parser.parse_args()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"JSON must be an object: {path}")
    return data


def _load_script_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _v30_requirements() -> Iterable[Any]:
    module = _load_script_module(
        "validate_v30_production_data_gate_for_v48_manifest",
        PROJECT_DIR / "scripts" / "validate_v30_production_data_gate.py",
    )
    return module._evidence_requirements()


def _status_for(key: str, provided: dict[str, str], missing: set[str]) -> str:
    if key in missing:
        return "missing"
    if provided.get(key):
        return "provided_pending_gate_validation"
    return "missing"


def _paper_progress(paper_health: dict[str, Any]) -> dict[str, Any]:
    progress = paper_health.get("progress", {})
    return progress if isinstance(progress, dict) else {}


def build_manifest(
    *,
    v48_gate: dict[str, Any],
    paper_health: dict[str, Any],
    generated_at: datetime,
) -> dict[str, Any]:
    external_refs = v48_gate.get("v44_external_refs", {})
    production_data_refs = v48_gate.get("production_data_refs", {})
    if not isinstance(external_refs, dict):
        external_refs = {}
    if not isinstance(production_data_refs, dict):
        production_data_refs = {}
    missing_external = set(v48_gate.get("missing_external_ref_keys", []))
    missing_v30 = set(v48_gate.get("missing_production_data_ref_keys", []))
    paper_progress = _paper_progress(paper_health)

    items: list[dict[str, Any]] = []
    for requirement in PRODUCTION_EVIDENCE_REQUIREMENTS:
        key = str(requirement.key)
        item = {
            "key": key,
            "gate": "V44/V48",
            "priority": PRIORITY.get(key, 99),
            "owner": V44_OWNER.get(key, "external-owner-required"),
            "description": requirement.description,
            "accepted_prefixes": list(requirement.allowed_prefixes),
            "current_status": _status_for(key, external_refs, missing_external),
            "current_ref_present": bool(external_refs.get(key)),
            "collection_action": COLLECTION_ACTIONS.get(
                key, "Provide approved external evidence reference."
            ),
            "cannot_be_satisfied_by": [
                "local file only",
                "mock/sample/todo reference",
                "secret value",
                "free-source approximation",
            ],
        }
        if key == "paper_trading_90d":
            item["paper_progress"] = paper_progress
        items.append(item)

    for requirement in _v30_requirements():
        key = str(requirement.key)
        items.append(
            {
                "key": key,
                "gate": "V30/V48",
                "priority": PRIORITY.get(key, 99),
                "owner": V30_OWNER.get(key, "data-owner-required"),
                "description": requirement.description,
                "accepted_prefixes": list(requirement.allowed_prefixes),
                "current_status": _status_for(key, production_data_refs, missing_v30),
                "current_ref_present": bool(production_data_refs.get(key)),
                "collection_action": COLLECTION_ACTIONS.get(
                    key, "Provide approved production data reference."
                ),
                "cannot_be_satisfied_by": [
                    "lianghua free-source PIT approximation",
                    "local parquet alone",
                    "quant-ashare data",
                    "mock/sample/todo reference",
                ],
            }
        )

    items.sort(key=lambda item: (int(item["priority"]), str(item["key"])))
    missing_count = sum(1 for item in items if item["current_status"] == "missing")
    return {
        "ts": generated_at.isoformat(),
        "version": "V48-external-evidence-collection-manifest",
        "production_ready": False,
        "evidence_collection_complete": missing_count == 0,
        "source_gate_valid": bool(v48_gate.get("evidence_bundle_valid")),
        "summary": {
            "total_items": len(items),
            "missing_items": missing_count,
            "provided_items": len(items) - missing_count,
            "paper_report_days": paper_progress.get("report_days"),
            "paper_calendar_days": paper_progress.get("calendar_days"),
            "paper_evidence_threshold_met": paper_progress.get("paper_evidence_threshold_met"),
        },
        "items": items,
        "next_actions": [
            "Collect V30 vendor/broker data evidence first; it is the earliest blocker for production-grade full-universe claims.",
            "Keep V49/V45 paper evidence cron and watchdog running until natural 90-calendar/60-report-day thresholds are met.",
            "Fill config/production_external_evidence_bundle.v48.template.json only with approved external refs, then rerun validate_v48_external_evidence_bundle.py.",
        ],
        "note": "This manifest is a checklist. It is not external evidence and does not reduce any production blocker by itself.",
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_report(path: Path, manifest: dict[str, Any]) -> None:
    summary = manifest["summary"]
    lines = [
        "# V48 外部证据收集清单",
        "",
        "状态：这是收集清单，不是生产证据，不解除任何 blocker。",
        "",
        "## 摘要",
        "",
        f"- 总项：{summary['total_items']}",
        f"- 缺失：{summary['missing_items']}",
        f"- 已提供待校验：{summary['provided_items']}",
        f"- paper report days：{summary['paper_report_days']}",
        f"- paper calendar days：{summary['paper_calendar_days']}",
        "",
        "## 收集顺序",
        "",
    ]
    for item in manifest["items"]:
        prefixes = ", ".join(f"`{prefix}`" for prefix in item["accepted_prefixes"])
        lines.extend(
            [
                f"### P{item['priority']} `{item['key']}`",
                "",
                f"- gate: {item['gate']}",
                f"- owner: {item['owner']}",
                f"- status: {item['current_status']}",
                f"- accepted prefixes: {prefixes}",
                f"- action: {item['collection_action']}",
                "",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    manifest = build_manifest(
        v48_gate=_load_json(Path(args.v48_gate_json)),
        paper_health=_load_json(Path(args.paper_health_json)),
        generated_at=datetime.now(timezone.utc),
    )
    _write_json(Path(args.output_json), manifest)
    _write_report(Path(args.report_md), manifest)
    print(
        json.dumps(
            {
                "production_ready": False,
                "evidence_collection_complete": manifest["evidence_collection_complete"],
                "missing_items": manifest["summary"]["missing_items"],
                "output": args.output_json,
                "report": args.report_md,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
