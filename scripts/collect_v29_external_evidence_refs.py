#!/usr/bin/env python3
"""Collect external evidence refs for the V29 paper/production gates.

The script never fabricates refs and never writes token/API-key material. It
merges an optional existing evidence JSON with explicit environment refs, can
probe selected HTTPS production adapters, and emits fail-closed evidence plus a
sanitized collection report.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _paths import PROJECT_DIR, RESULTS_DIR

sys.path.insert(0, str(PROJECT_DIR / "src"))

from quant_trading.config.settings import AccountConfig, Environment, Market, QuantSettings
from quant_trading.deployment import (
    PRODUCTION_EVIDENCE_REQUIREMENTS,
    evaluate_production_readiness,
)
from quant_trading.execution.order_manager import Order, OrderSide
from quant_trading.integrations import (
    HttpApprovalServiceValidator,
    HttpBorrowAvailabilityProvider,
    HttpExchangePositionProvider,
    HttpSecretManagerResolver,
    HttpServiceConfig,
    ProductionIntegrationError,
)
from quant_trading.strategy.v29_spec import PAPER_START_REQUIRED_KEYS

DEFAULT_TEMPLATE_JSON = PROJECT_DIR / "config" / "production_evidence.v29.template.json"
DEFAULT_OUTPUT_EVIDENCE_JSON = RESULTS_DIR / "quant_v29_production_evidence_collected.json"
DEFAULT_OUTPUT_REPORT_JSON = RESULTS_DIR / "quant_v29_external_evidence_collection_report.json"

EVIDENCE_KEYS: tuple[str, ...] = tuple(
    requirement.key for requirement in PRODUCTION_EVIDENCE_REQUIREMENTS
)

ENV_REF_NAMES: dict[str, tuple[str, ...]] = {
    key: (
        f"QUANT_EVIDENCE_{key.upper()}_REF",
        f"QUANT_{key.upper()}_REF",
    )
    for key in EVIDENCE_KEYS
}


class EvidenceCollectionError(RuntimeError):
    """Raised when the collector is asked to require unavailable evidence."""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template-json", default=str(DEFAULT_TEMPLATE_JSON))
    parser.add_argument(
        "--input-json",
        default=os.getenv("QUANT_PRODUCTION_EVIDENCE_FILE", ""),
        help="Optional existing evidence refs JSON to merge before env refs.",
    )
    parser.add_argument("--output-evidence-json", default=str(DEFAULT_OUTPUT_EVIDENCE_JSON))
    parser.add_argument("--output-report-json", default=str(DEFAULT_OUTPUT_REPORT_JSON))
    parser.add_argument(
        "--probe-services",
        action="store_true",
        help="Probe configured HTTPS services when enough non-secret config and token env vars exist.",
    )
    parser.add_argument(
        "--require-paper-launch-ready",
        action="store_true",
        help="Exit non-zero unless V29 paper-start evidence is structurally trusted.",
    )
    parser.add_argument(
        "--require-production-ready",
        action="store_true",
        help="Exit non-zero unless all production evidence is structurally trusted.",
    )
    return parser.parse_args()


def _resolve(path_value: str, default_dir: Path) -> Path:
    path = Path(path_value)
    if path.is_absolute() or path.parent != Path("."):
        return path
    return default_dir / path


def _read_json_object(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise EvidenceCollectionError(f"evidence JSON must be an object: {path}")
    return {str(key): str(value) for key, value in data.items()}


def _env_value(env: Mapping[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        value = env.get(name, "").strip()
        if value:
            return value
    return ""


def collect_direct_refs(
    base: Mapping[str, str],
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Merge base refs with explicit environment refs without accepting secrets."""

    env = env or os.environ
    evidence = {key: str(base.get(key, "")).strip() for key in EVIDENCE_KEYS}
    for key, env_names in ENV_REF_NAMES.items():
        value = _env_value(env, env_names)
        if value:
            evidence[key] = value
    return evidence


def _token_provider(env_name: str) -> Callable[[], str]:
    def provider() -> str:
        return os.getenv(env_name, "").strip()

    return provider


def _probe_secret_manager(evidence: dict[str, str]) -> dict[str, Any]:
    url = os.getenv("QUANT_SECRET_MANAGER_URL", "").strip()
    token_env = "QUANT_SECRET_MANAGER_TOKEN"
    secret_ref = os.getenv("QUANT_SECRET_MANAGER_SECRET_REF", evidence.get("secret_manager", "")).strip()
    account_id = os.getenv("QUANT_SECRET_MANAGER_ACCOUNT_ID", "v29-paper-account").strip()
    exchange = os.getenv("QUANT_SECRET_MANAGER_EXCHANGE", "paper").strip()
    if not url or not secret_ref or not os.getenv(token_env, "").strip():
        return {"attempted": False, "reason": "missing url/token/secret_ref"}
    resolver = HttpSecretManagerResolver(
        HttpServiceConfig(url),
        token_provider=_token_provider(token_env),
    )
    account = AccountConfig(
        account_id=account_id,
        name="v29-paper-secret-probe",
        exchange=exchange,
        environment=Environment.PROD,
        secret_ref=secret_ref,
    )
    resolver(account)
    evidence["secret_manager"] = secret_ref
    return {"attempted": True, "passed": True, "evidence_ref": secret_ref}


def _probe_approval_service(evidence: dict[str, str]) -> dict[str, Any]:
    url = os.getenv("QUANT_APPROVAL_SERVICE_URL", "").strip()
    token_env = "QUANT_APPROVAL_SERVICE_TOKEN"
    config_hash = os.getenv("QUANT_APPROVAL_CONFIG_HASH", "").strip()
    requested_by = os.getenv("QUANT_APPROVAL_REQUESTED_BY", "risk-officer").strip()
    if not url or not config_hash or not os.getenv(token_env, "").strip():
        return {"attempted": False, "reason": "missing url/token/config_hash"}
    validator = HttpApprovalServiceValidator(
        HttpServiceConfig(url),
        requested_by=requested_by,
        token_provider=_token_provider(token_env),
    )
    approval_ref = validator(QuantSettings(config_hash=config_hash))
    evidence["approval_service"] = approval_ref
    return {"attempted": True, "passed": True, "evidence_ref": approval_ref}


def _probe_position_provider(evidence: dict[str, str]) -> dict[str, Any]:
    url = os.getenv("QUANT_POSITION_PROVIDER_URL", "").strip()
    token_env = "QUANT_POSITION_PROVIDER_TOKEN"
    provider_ref = os.getenv(
        "QUANT_EVIDENCE_EXCHANGE_POSITION_PROVIDER_REF",
        evidence.get("exchange_position_provider", ""),
    ).strip()
    account_id = os.getenv("QUANT_POSITION_PROVIDER_ACCOUNT_ID", "v29-paper-account").strip()
    symbol = os.getenv("QUANT_POSITION_PROVIDER_PROBE_SYMBOL", "600519.SH").strip()
    if not url or not provider_ref or not os.getenv(token_env, "").strip():
        return {"attempted": False, "reason": "missing url/token/provider_ref"}
    provider = HttpExchangePositionProvider(
        HttpServiceConfig(url),
        account_id=account_id,
        token_provider=_token_provider(token_env),
    )
    provider(Order("v29-evidence-probe", symbol, OrderSide.SELL, 100, market=Market.A_SHARES))
    evidence["exchange_position_provider"] = provider_ref
    return {"attempted": True, "passed": True, "evidence_ref": provider_ref}


def _probe_borrow_provider(evidence: dict[str, str]) -> dict[str, Any]:
    url = os.getenv("QUANT_BORROW_PROVIDER_URL", "").strip()
    token_env = "QUANT_BORROW_PROVIDER_TOKEN"
    account_id = os.getenv("QUANT_BORROW_PROVIDER_ACCOUNT_ID", "v29-paper-account").strip()
    symbol = os.getenv("QUANT_BORROW_PROVIDER_PROBE_SYMBOL", "600519.SH").strip()
    if not url or not os.getenv(token_env, "").strip():
        return {"attempted": False, "reason": "missing url/token"}
    provider = HttpBorrowAvailabilityProvider(
        HttpServiceConfig(url),
        account_id=account_id,
        token_provider=_token_provider(token_env),
    )
    snapshot = provider(symbol)
    evidence["broker_borrow_availability"] = snapshot.evidence_ref
    return {"attempted": True, "passed": True, "evidence_ref": snapshot.evidence_ref}


def probe_configured_services(evidence: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Optionally validate service reachability; tokens never enter outputs."""

    probes = {
        "secret_manager_probe": _probe_secret_manager,
        "approval_service_probe": _probe_approval_service,
        "exchange_position_provider_probe": _probe_position_provider,
        "borrow_provider_probe": _probe_borrow_provider,
    }
    results: dict[str, dict[str, Any]] = {}
    for name, probe in probes.items():
        try:
            results[name] = probe(evidence)
        except (ProductionIntegrationError, ValueError) as exc:
            results[name] = {"attempted": True, "passed": False, "error_type": type(exc).__name__}
    return results


def _structural_blockers(evidence: Mapping[str, str], keys: tuple[str, ...]) -> list[str]:
    requirements = {requirement.key: requirement for requirement in PRODUCTION_EVIDENCE_REQUIREMENTS}
    blockers: list[str] = []
    for key in keys:
        blocker = requirements[key].validate(evidence)
        if blocker:
            blockers.append(blocker)
    return blockers


def build_collection_report(
    evidence: Mapping[str, str],
    *,
    service_probe_results: Mapping[str, Any] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or datetime.now(timezone.utc)
    paper_blockers = _structural_blockers(evidence, PAPER_START_REQUIRED_KEYS)
    production_report = evaluate_production_readiness(evidence)
    return {
        "ts": generated_at.isoformat(),
        "version": "V29-external-evidence-ref-collection",
        "no_secret_values_written": True,
        "provided_keys": sorted(key for key, value in evidence.items() if str(value).strip()),
        "paper_start_required_keys": list(PAPER_START_REQUIRED_KEYS),
        "production_required_keys": list(EVIDENCE_KEYS),
        "paper_launch_evidence_ready": not paper_blockers,
        "production_evidence_ready": production_report.passed,
        "paper_launch_blockers": paper_blockers,
        "production_blockers": list(production_report.blockers),
        "missing_for_paper_start": [key for key in PAPER_START_REQUIRED_KEYS if not evidence.get(key, "")],
        "missing_for_full_production": [key for key in EVIDENCE_KEYS if not evidence.get(key, "")],
        "service_probe_results": service_probe_results or {},
        "next_commands": [
            "scripts/validate_external_evidence_v26.py --evidence-file <collected-evidence-json>",
            "scripts/prepare_v29_paper_launch_package.py --evidence-file <collected-evidence-json> --require-paper-launch-ready",
        ],
    }


def write_outputs(
    evidence: Mapping[str, str],
    report: Mapping[str, Any],
    *,
    output_evidence_path: Path,
    output_report_path: Path,
) -> None:
    output_evidence_path.parent.mkdir(parents=True, exist_ok=True)
    output_report_path.parent.mkdir(parents=True, exist_ok=True)
    output_evidence_path.write_text(
        json.dumps({key: evidence.get(key, "") for key in EVIDENCE_KEYS}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    output_report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    template_path = _resolve(args.template_json, PROJECT_DIR)
    input_path = _resolve(args.input_json, PROJECT_DIR) if args.input_json else None
    output_evidence_path = _resolve(args.output_evidence_json, RESULTS_DIR)
    output_report_path = _resolve(args.output_report_json, RESULTS_DIR)

    base = _read_json_object(template_path)
    if input_path is not None:
        base.update(_read_json_object(input_path))
    evidence = collect_direct_refs(base)
    service_probe_results = probe_configured_services(evidence) if args.probe_services else {}
    report = build_collection_report(evidence, service_probe_results=service_probe_results)
    write_outputs(
        evidence,
        report,
        output_evidence_path=output_evidence_path,
        output_report_path=output_report_path,
    )
    print(
        "V29 external evidence collection: "
        f"paper_ready={report['paper_launch_evidence_ready']} "
        f"production_ready={report['production_evidence_ready']} "
        f"paper_blockers={len(report['paper_launch_blockers'])} "
        f"production_blockers={len(report['production_blockers'])}",
        flush=True,
    )
    print(f"Wrote {output_evidence_path}", flush=True)
    print(f"Wrote {output_report_path}", flush=True)
    if args.require_paper_launch_ready and not report["paper_launch_evidence_ready"]:
        raise SystemExit(1)
    if args.require_production_ready and not report["production_evidence_ready"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
