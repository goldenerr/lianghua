#!/usr/bin/env python3
"""Validate raw paper-shadow source exports before V45 ingestion."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

from _paths import PROJECT_DIR, RESULTS_DIR

DEFAULT_OUTPUT = RESULTS_DIR / "quant_v47_paper_shadow_source_export_gate.json"
DEFAULT_REPORT = Path("docs/research/quant_v47_paper_shadow_source_export_gate.md")
DEFAULT_DAILY_OUTPUT = RESULTS_DIR / "quant_v47_validated_paper_shadow_daily_report.json"

TRUSTED_ARCHIVE_PREFIXES = ("worm://", "archive://", "s3://worm-")
TRUSTED_PROVIDER_PREFIXES = ("provider://", "vendor://")
TRUSTED_BROKER_PREFIXES = ("broker://", "exchange://")
SENSITIVE_KEY_RE = re.compile(r"(^|[_-])(token|password|api[_-]?key|private[_-]?key)([_-]|$)", re.I)
SENSITIVE_VALUE_RE = re.compile(r"(Bearer\s+[A-Za-z0-9._=-]+|sk-[A-Za-z0-9]{12,}|AKIA[0-9A-Z]{16})")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-export-json", default="")
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--report-md", default=str(DEFAULT_REPORT))
    parser.add_argument("--daily-output-json", default=str(DEFAULT_DAILY_OUTPUT))
    parser.add_argument("--write-daily-output", action="store_true")
    parser.add_argument("--require-source-valid", action="store_true")
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
            if SENSITIVE_KEY_RE.search(key):
                blockers.append(f"发现疑似敏感字段：{child_path}")
            blockers.extend(_scan_sensitive_material(value, child_path))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            blockers.extend(_scan_sensitive_material(value, f"{path}[{index}]"))
    elif isinstance(node, str) and SENSITIVE_VALUE_RE.search(node):
        blockers.append(f"发现疑似敏感值：{path}")
    return blockers


def _finite_float(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是数字") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} 必须是有限数字")
    return result


def _int_like(value: Any, field: str) -> int:
    result = _finite_float(value, field)
    if abs(result - round(result)) > 1e-9:
        raise ValueError(f"{field} 必须是整数")
    return int(round(result))


def _parse_date(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("date 必须是 YYYY-MM-DD 字符串")
    return date.fromisoformat(value.strip()).isoformat()


def _require_section(source: dict[str, Any], key: str, blockers: list[str]) -> dict[str, Any]:
    value = source.get(key)
    if not isinstance(value, dict):
        blockers.append(f"{key} 必须存在且为对象")
        return {}
    return value


def _require_list(source: dict[str, Any], key: str, blockers: list[str]) -> list[Any]:
    value = source.get(key)
    if not isinstance(value, list):
        blockers.append(f"{key} 必须存在且为列表")
        return []
    return value


def _trusted_ref(value: Any, prefixes: tuple[str, ...], field: str, blockers: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        blockers.append(f"{field} 不能为空")
        return
    lowered = value.lower()
    if any(token in lowered for token in ("mock", "pending", "todo", "sample")):
        blockers.append(f"{field} 不能包含 mock/pending/todo/sample")
    if not value.startswith(prefixes):
        blockers.append(f"{field} 前缀不可信")


def _validate_controls(source: dict[str, Any], blockers: list[str]) -> None:
    controls = _require_section(source, "controls", blockers)
    account = source.get("account", {})
    if account and not isinstance(account, dict):
        blockers.append("account 必须是对象")
        account = {}
    for container_name, container in (("controls", controls), ("account", account)):
        for key in ("auto_trade_enabled", "live_order_submission_allowed"):
            value = bool(container.get(key, False))
            if value:
                blockers.append(f"{container_name}.{key} 必须为 false")
    if str(account.get("account_type", "paper")).lower() not in {"paper", "shadow", "sim"}:
        blockers.append("account.account_type 必须是 paper/shadow/sim")


def _validate_returns_costs_risk(source: dict[str, Any], blockers: list[str]) -> None:
    returns = _require_section(source, "returns", blockers)
    costs = _require_section(source, "costs", blockers)
    risk = _require_section(source, "risk", blockers)
    numeric_fields = (
        (returns, "paper_daily_return"),
        (returns, "backtest_daily_return"),
        (costs, "actual_slippage_bps"),
        (costs, "expected_slippage_bps"),
        (costs, "actual_cost_bps"),
        (costs, "expected_cost_bps"),
    )
    for section, field in numeric_fields:
        try:
            _finite_float(section.get(field), field)
        except ValueError as exc:
            blockers.append(str(exc))
    for field in ("risk_capacity_violations", "invariant_violations"):
        try:
            value = _int_like(risk.get(field, 0), field)
        except ValueError as exc:
            blockers.append(str(exc))
            continue
        if value != 0:
            blockers.append(f"{field} 必须为 0")


def _validate_refs(source: dict[str, Any], blockers: list[str]) -> None:
    refs = _require_section(source, "refs", blockers)
    _trusted_ref(refs.get("archive_ref"), TRUSTED_ARCHIVE_PREFIXES, "refs.archive_ref", blockers)
    _trusted_ref(refs.get("market_data_ref"), TRUSTED_PROVIDER_PREFIXES, "refs.market_data_ref", blockers)
    _trusted_ref(
        refs.get("position_snapshot_ref"),
        TRUSTED_BROKER_PREFIXES,
        "refs.position_snapshot_ref",
        blockers,
    )
    _trusted_ref(refs.get("order_fill_log_ref"), TRUSTED_BROKER_PREFIXES, "refs.order_fill_log_ref", blockers)


def _validate_orders_and_fills(
    source: dict[str, Any],
    blockers: list[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, float], set[str]]:
    orders_raw = _require_list(source, "orders", blockers)
    fills_raw = _require_list(source, "fills", blockers)
    order_by_id: dict[str, dict[str, Any]] = {}
    fill_qty_by_order: dict[str, float] = defaultdict(float)
    net_fill_by_symbol: dict[str, float] = defaultdict(float)
    traded_symbols: set[str] = set()

    for index, raw_order in enumerate(orders_raw):
        if not isinstance(raw_order, dict):
            blockers.append(f"orders[{index}] 必须是对象")
            continue
        client_order_id = str(raw_order.get("client_order_id", "")).strip()
        if not client_order_id:
            blockers.append(f"orders[{index}].client_order_id 不能为空")
            continue
        if client_order_id in order_by_id:
            blockers.append(f"重复 client_order_id：{client_order_id}")
        symbol = str(raw_order.get("symbol", "")).strip()
        side = str(raw_order.get("side", "")).upper()
        try:
            quantity = _finite_float(raw_order.get("quantity"), f"orders[{index}].quantity")
        except ValueError as exc:
            blockers.append(str(exc))
            quantity = 0.0
        if not symbol:
            blockers.append(f"orders[{index}].symbol 不能为空")
        if side not in {"BUY", "SELL"}:
            blockers.append(f"orders[{index}].side 必须是 BUY 或 SELL")
        if quantity <= 0:
            blockers.append(f"orders[{index}].quantity 必须大于 0")
        order_by_id[client_order_id] = {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
        }
        if symbol:
            traded_symbols.add(symbol)

    for index, raw_fill in enumerate(fills_raw):
        if not isinstance(raw_fill, dict):
            blockers.append(f"fills[{index}] 必须是对象")
            continue
        client_order_id = str(raw_fill.get("client_order_id", "")).strip()
        order = order_by_id.get(client_order_id)
        if order is None:
            blockers.append(f"fills[{index}] 找不到对应订单：{client_order_id}")
            continue
        symbol = str(raw_fill.get("symbol", "")).strip()
        side = str(raw_fill.get("side", "")).upper()
        try:
            quantity = _finite_float(raw_fill.get("quantity"), f"fills[{index}].quantity")
            price = _finite_float(raw_fill.get("price"), f"fills[{index}].price")
        except ValueError as exc:
            blockers.append(str(exc))
            continue
        if symbol != order["symbol"]:
            blockers.append(f"fills[{index}] symbol 与订单不一致")
        if side != order["side"]:
            blockers.append(f"fills[{index}] side 与订单不一致")
        if quantity <= 0:
            blockers.append(f"fills[{index}].quantity 必须大于 0")
        if price <= 0:
            blockers.append(f"fills[{index}].price 必须大于 0")
        fill_qty_by_order[client_order_id] += quantity
        sign = 1.0 if side == "BUY" else -1.0
        net_fill_by_symbol[symbol] += sign * quantity
        traded_symbols.add(symbol)

    for client_order_id, fill_qty in fill_qty_by_order.items():
        order_qty = float(order_by_id[client_order_id]["quantity"])
        if fill_qty - order_qty > 1e-9:
            blockers.append(f"订单成交数量超过委托数量：{client_order_id}")
    return order_by_id, dict(net_fill_by_symbol), traded_symbols


def _validate_positions(
    source: dict[str, Any],
    net_fill_by_symbol: dict[str, float],
    traded_symbols: set[str],
    blockers: list[str],
) -> set[str]:
    positions_raw = _require_list(source, "positions", blockers)
    position_symbols: set[str] = set()
    for index, raw_position in enumerate(positions_raw):
        if not isinstance(raw_position, dict):
            blockers.append(f"positions[{index}] 必须是对象")
            continue
        symbol = str(raw_position.get("symbol", "")).strip()
        if not symbol:
            blockers.append(f"positions[{index}].symbol 不能为空")
            continue
        position_symbols.add(symbol)
        try:
            previous = _finite_float(raw_position.get("previous_quantity"), "previous_quantity")
            fill_delta = _finite_float(raw_position.get("fill_delta_quantity"), "fill_delta_quantity")
            end_quantity = _finite_float(raw_position.get("end_quantity"), "end_quantity")
        except ValueError as exc:
            blockers.append(f"positions[{index}]: {exc}")
            continue
        computed_delta = net_fill_by_symbol.get(symbol, 0.0)
        if abs(fill_delta - computed_delta) > 1e-6:
            blockers.append(f"positions[{index}] fill_delta_quantity 与成交净变化不一致")
        if abs(previous + fill_delta - end_quantity) > 1e-6:
            blockers.append(f"positions[{index}] previous + fill_delta != end_quantity")
    missing_positions = sorted(symbol for symbol in traded_symbols if symbol and symbol not in position_symbols)
    if missing_positions:
        blockers.append(f"成交品种缺少持仓快照：{','.join(missing_positions[:10])}")
    return position_symbols


def _validate_market_data(
    source: dict[str, Any],
    required_symbols: set[str],
    blockers: list[str],
) -> None:
    market_data = _require_section(source, "market_data", blockers)
    refs = source.get("refs", {}) if isinstance(source.get("refs"), dict) else {}
    ref = str(market_data.get("ref", "")).strip()
    if ref and refs.get("market_data_ref") and ref != refs.get("market_data_ref"):
        blockers.append("market_data.ref 必须与 refs.market_data_ref 一致")
    symbols_raw = market_data.get("symbols", [])
    if not isinstance(symbols_raw, list):
        blockers.append("market_data.symbols 必须是列表")
        symbols_raw = []
    available_symbols = {str(symbol).strip() for symbol in symbols_raw if str(symbol).strip()}
    missing = sorted(required_symbols - available_symbols)
    if missing:
        blockers.append(f"行情导出缺少品种：{','.join(missing[:10])}")


def _validate_audit(source: dict[str, Any], blockers: list[str]) -> dict[str, Any]:
    audit = _require_section(source, "audit", blockers)
    result = {"audit_event_count": 0, "audit_hash": ""}
    if isinstance(audit.get("audit_events"), list):
        events = audit["audit_events"]
        result["audit_event_count"] = len(events)
        material = "\n".join(
            json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for event in events
        )
        result["audit_hash"] = hashlib.sha256((material + ("\n" if events else "")).encode()).hexdigest()
        if not events:
            blockers.append("audit.audit_events 不能为空")
        for index, event in enumerate(events):
            if not isinstance(event, dict) or not event.get("event_type"):
                blockers.append(f"audit.audit_events[{index}] 必须包含 event_type")
    elif audit.get("audit_hash"):
        audit_hash = str(audit.get("audit_hash", "")).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", audit_hash):
            blockers.append("audit.audit_hash 必须是 64 位 sha256")
        result["audit_hash"] = audit_hash
        try:
            result["audit_event_count"] = _int_like(audit.get("audit_event_count"), "audit_event_count")
        except ValueError as exc:
            blockers.append(str(exc))
            result["audit_event_count"] = 0
        if result["audit_event_count"] <= 0:
            blockers.append("audit.audit_event_count 必须大于 0")
    else:
        blockers.append("audit 必须包含 audit_events 或 audit_hash")
    return result


def validate_source_export(source: dict[str, Any]) -> dict[str, Any]:
    blockers: list[str] = []
    blockers.extend(_scan_sensitive_material(source))
    try:
        report_date = _parse_date(source.get("date"))
    except ValueError as exc:
        blockers.append(str(exc))
        report_date = ""
    _validate_controls(source, blockers)
    _validate_returns_costs_risk(source, blockers)
    _validate_refs(source, blockers)
    _, net_fill_by_symbol, traded_symbols = _validate_orders_and_fills(source, blockers)
    position_symbols = _validate_positions(source, net_fill_by_symbol, traded_symbols, blockers)
    _validate_market_data(source, traded_symbols | position_symbols, blockers)
    audit_metrics = _validate_audit(source, blockers)
    source_export_valid = not blockers
    return {
        "source_export_valid": source_export_valid,
        "production_ready": False,
        "date": report_date,
        "blockers": blockers,
        "metrics": {
            "order_count": len(source.get("orders", [])) if isinstance(source.get("orders"), list) else 0,
            "fill_count": len(source.get("fills", [])) if isinstance(source.get("fills"), list) else 0,
            "position_count": len(source.get("positions", []))
            if isinstance(source.get("positions"), list)
            else 0,
            "traded_symbol_count": len(traded_symbols),
            "position_symbol_count": len(position_symbols),
            **audit_metrics,
        },
        "note": "V47 只校验源导出是否可进入 V46/V45，不代表生产批准。",
    }


def _build_daily_report(source: dict[str, Any]) -> dict[str, Any]:
    v46_module = _load_script_module(
        "generate_v46_paper_shadow_daily_template_runtime",
        PROJECT_DIR / "scripts" / "generate_v46_paper_shadow_daily_template.py",
    )
    return v46_module.build_daily_report_from_export(source)


def _write_report(path: Path, payload: dict[str, Any], daily_output: Path | None) -> None:
    lines = [
        "# V47 影子模拟盘源导出校验器",
        "",
        "状态：源导出校验已生成，当前不代表生产批准。",
        "",
        "## 结论",
        "",
        f"- `source_export_valid={str(payload['source_export_valid']).lower()}`",
        "- `production_ready=false`",
        f"- 日期：{payload.get('date') or '无'}",
        f"- 阻塞项数量：{len(payload['blockers'])}",
        "",
        "## 指标",
        "",
    ]
    for key, value in payload["metrics"].items():
        lines.append(f"- `{key}`：{value}")
    lines.extend(["", "## 阻塞项", ""])
    if payload["blockers"]:
        lines.extend(f"- {item}" for item in payload["blockers"])
    else:
        lines.append("- 无")
    lines.extend(["", "## 输出", ""])
    if daily_output is not None:
        lines.append(f"- 已生成可交给 V45 的单日日报：`{daily_output}`")
    else:
        lines.append("- 未生成单日日报")
    lines.extend(
        [
            "",
            "## 说明",
            "",
            "- V47 校验订单、成交、持仓、行情和审计导出的一致性。",
            "- V47 通过只表示可以进入 V46/V45 流水线，不代表 V42/V44 或生产准入通过。",
            "- 任何敏感字段、实盘下单权限、订单成交不一致或持仓对账不一致都会阻塞。",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    if args.source_export_json:
        source = _load_json_object(Path(args.source_export_json))
        payload = validate_source_export(source)
    else:
        source = {}
        payload = {
            "source_export_valid": False,
            "production_ready": False,
            "date": "",
            "blockers": ["缺少 --source-export-json"],
            "metrics": {
                "order_count": 0,
                "fill_count": 0,
                "position_count": 0,
                "traded_symbol_count": 0,
                "position_symbol_count": 0,
                "audit_event_count": 0,
                "audit_hash": "",
            },
            "note": "V47 只校验源导出是否可进入 V46/V45，不代表生产批准。",
        }
    daily_output_path: Path | None = None
    if args.write_daily_output and payload["source_export_valid"]:
        daily_output_path = Path(args.daily_output_json)
        daily_output_path.parent.mkdir(parents=True, exist_ok=True)
        daily_output_path.write_text(
            json.dumps(_build_daily_report(source), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    output = Path(args.output_json)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(Path(args.report_md), payload, daily_output_path)
    print(
        json.dumps(
            {
                "production_ready": False,
                "source_export_valid": payload["source_export_valid"],
                "blocker_count": len(payload["blockers"]),
                "output": str(output),
                "daily_output": str(daily_output_path) if daily_output_path else "",
                "report": str(Path(args.report_md)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.require_source_valid and not payload["source_export_valid"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
