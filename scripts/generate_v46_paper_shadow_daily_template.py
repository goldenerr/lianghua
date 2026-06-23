#!/usr/bin/env python3
"""Generate V45 paper-shadow daily report templates and adapter mapping."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _paths import RESULTS_DIR

DEFAULT_TEMPLATE = RESULTS_DIR / "quant_v46_paper_shadow_daily_template.json"
DEFAULT_EXAMPLE = RESULTS_DIR / "quant_v46_paper_shadow_daily_example.json"
DEFAULT_MAPPING = RESULTS_DIR / "quant_v46_paper_shadow_adapter_mapping.json"
DEFAULT_DAILY_OUTPUT = RESULTS_DIR / "quant_v46_paper_shadow_daily_from_export.json"
DEFAULT_REPORT = Path("docs/research/quant_v46_paper_shadow_daily_template.md")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template-json", default=str(DEFAULT_TEMPLATE))
    parser.add_argument("--example-json", default=str(DEFAULT_EXAMPLE))
    parser.add_argument("--mapping-json", default=str(DEFAULT_MAPPING))
    parser.add_argument("--report-md", default=str(DEFAULT_REPORT))
    parser.add_argument(
        "--source-export-json",
        default="",
        help="Optional normalized broker/provider export to convert into a V45 daily report.",
    )
    parser.add_argument("--daily-output-json", default=str(DEFAULT_DAILY_OUTPUT))
    return parser.parse_args()


def build_template() -> dict[str, Any]:
    return {
        "template_only": True,
        "version": "V46-paper-shadow-daily-template",
        "说明": "这是模板，V45 会拒绝 template_only=true 的文件，不能作为真实证据入账。",
        "external_refs": {
            "paper_trading_90d": "paper://真实90天影子模拟盘报告引用",
        },
        "daily_report": {
            "date": "YYYY-MM-DD",
            "paper_daily_return": 0.0,
            "backtest_daily_return": 0.0,
            "actual_slippage_bps": 0.0,
            "expected_slippage_bps": 0.0,
            "actual_cost_bps": 0.0,
            "expected_cost_bps": 0.0,
            "risk_capacity_violations": 0,
            "invariant_violations": 0,
            "audit_hash": "64位sha256审计日志哈希",
            "audit_event_count": 0,
            "archive_ref": "worm://或archive://或s3://worm-真实归档引用",
            "market_data_ref": "provider://或vendor://真实行情源引用",
            "position_snapshot_ref": "broker://或exchange://真实持仓快照引用",
            "order_fill_log_ref": "broker://或exchange://真实订单成交日志引用",
            "auto_trade_enabled": False,
            "live_order_submission_allowed": False,
        },
    }


def build_example() -> dict[str, Any]:
    return {
        "example_only": True,
        "version": "V46-paper-shadow-daily-example",
        "说明": "这是示例，V45 会拒绝 example_only=true 的文件，不能作为真实证据入账。",
        "external_refs": {
            "paper_trading_90d": "paper://example-only/not-production-evidence",
        },
        "daily_report": {
            "date": "2026-01-02",
            "paper_daily_return": 0.001,
            "backtest_daily_return": 0.0011,
            "actual_slippage_bps": 2.0,
            "expected_slippage_bps": 3.0,
            "actual_cost_bps": 4.0,
            "expected_cost_bps": 5.0,
            "risk_capacity_violations": 0,
            "invariant_violations": 0,
            "audit_events": [
                {"event_type": "paper_signal", "trace_id": "example", "payload": {"orders": 0}},
                {"event_type": "paper_reconcile", "trace_id": "example", "payload": {"ok": True}},
            ],
            "archive_ref": "archive://example-only/not-production-evidence",
            "market_data_ref": "provider://example-only/not-production-evidence",
            "position_snapshot_ref": "broker://example-only/not-production-evidence",
            "order_fill_log_ref": "broker://example-only/not-production-evidence",
            "auto_trade_enabled": False,
            "live_order_submission_allowed": False,
        },
    }


def build_mapping() -> dict[str, Any]:
    return {
        "version": "V46-paper-shadow-adapter-mapping",
        "production_ready": False,
        "说明": "映射说明只用于把真实导出转换为 V45 日报，不是证据。",
        "source_sections": {
            "returns": {
                "paper_daily_return": "扣除费用和滑点后的模拟盘单日收益",
                "backtest_daily_return": "同日期对齐的回测预期单日收益",
            },
            "costs": {
                "actual_slippage_bps": "模拟成交实际滑点，单位 bps",
                "expected_slippage_bps": "回测滑点模型预期值，单位 bps",
                "actual_cost_bps": "佣金、税费、费用和滑点合计实际成本，单位 bps",
                "expected_cost_bps": "回测总成本模型预期值，单位 bps",
            },
            "risk": {
                "risk_capacity_violations": "容量或风险违规次数",
                "invariant_violations": "持仓、权益或幂等性不变式违规次数",
            },
            "refs": {
                "archive_ref": "每日审计日志的 WORM 或归档引用",
                "market_data_ref": "已批准行情供应商的每日数据引用",
                "position_snapshot_ref": "券商或交易所持仓快照引用",
                "order_fill_log_ref": "券商或交易所订单/成交日志引用",
                "paper_trading_90d": "具备时填写滚动 90 天模拟盘证据引用",
            },
            "audit": {
                "audit_hash": "可选 sha256 哈希",
                "audit_event_count": "可选事件数量",
                "audit_events": "可选审计事件列表，V45 可据此计算哈希",
                "audit_jsonl_path": "可选本地审计 JSONL 路径，V45 可据此计算哈希",
            },
        },
        "normalized_source_export_shape": {
            "date": "YYYY-MM-DD",
            "returns": {"paper_daily_return": 0.0, "backtest_daily_return": 0.0},
            "costs": {
                "actual_slippage_bps": 0.0,
                "expected_slippage_bps": 0.0,
                "actual_cost_bps": 0.0,
                "expected_cost_bps": 0.0,
            },
            "risk": {"risk_capacity_violations": 0, "invariant_violations": 0},
            "refs": {
                "archive_ref": "worm://...",
                "market_data_ref": "provider://...",
                "position_snapshot_ref": "broker://...",
                "order_fill_log_ref": "broker://...",
                "paper_trading_90d": "paper://...",
            },
            "audit": {"audit_events": []},
            "controls": {
                "auto_trade_enabled": False,
                "live_order_submission_allowed": False,
            },
        },
    }


def _required_section(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"{key} 必须是对象")
    return value


def build_daily_report_from_export(source: dict[str, Any]) -> dict[str, Any]:
    returns = _required_section(source, "returns")
    costs = _required_section(source, "costs")
    risk = _required_section(source, "risk")
    refs = _required_section(source, "refs")
    audit = _required_section(source, "audit")
    controls = _required_section(source, "controls")
    daily_report = {
        "date": source.get("date"),
        "paper_daily_return": returns.get("paper_daily_return"),
        "backtest_daily_return": returns.get("backtest_daily_return"),
        "actual_slippage_bps": costs.get("actual_slippage_bps"),
        "expected_slippage_bps": costs.get("expected_slippage_bps"),
        "actual_cost_bps": costs.get("actual_cost_bps"),
        "expected_cost_bps": costs.get("expected_cost_bps"),
        "risk_capacity_violations": risk.get("risk_capacity_violations", 0),
        "invariant_violations": risk.get("invariant_violations", 0),
        "archive_ref": refs.get("archive_ref"),
        "market_data_ref": refs.get("market_data_ref"),
        "position_snapshot_ref": refs.get("position_snapshot_ref"),
        "order_fill_log_ref": refs.get("order_fill_log_ref"),
        "auto_trade_enabled": controls.get("auto_trade_enabled", False),
        "live_order_submission_allowed": controls.get("live_order_submission_allowed", False),
    }
    for key in ("audit_hash", "audit_event_count", "audit_events", "audit_jsonl_path"):
        if key in audit:
            daily_report[key] = audit[key]
    external_refs = {}
    if refs.get("paper_trading_90d"):
        external_refs["paper_trading_90d"] = refs["paper_trading_90d"]
    return {
        "version": "V46-paper-shadow-daily-from-export",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "external_refs": external_refs,
        "daily_report": daily_report,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_report(path: Path, *, template: Path, example: Path, mapping: Path, daily_output: Path | None) -> None:
    lines = [
        "# V46 影子模拟盘日报模板与导出适配器",
        "",
        "状态：模板和适配器已生成，当前不代表生产批准。",
        "",
        "## 产物",
        "",
        f"- 模板：`{template}`",
        f"- 示例：`{example}`",
        f"- 字段映射：`{mapping}`",
    ]
    if daily_output is not None:
        lines.append(f"- 本次转换日报：`{daily_output}`")
    lines.extend(
        [
            "",
            "## 安全约束",
            "",
            "- 模板带有 `template_only=true`，V45 会拒绝写入台账。",
            "- 示例带有 `example_only=true`，V45 会拒绝写入台账。",
            "- 默认不会生成可入账日报；只有显式提供 `--source-export-json` 才会转换真实导出文件。",
            "- 转换出的日报仍必须经过 V45、V42 和 V44 逐级校验。",
            "- 任何令牌、接口密钥、密码或私钥都不能出现在日报或映射文件中。",
            "",
            "## 使用流程",
            "",
            "- 从真实模拟盘服务导出标准化源 JSON。",
            "- 运行本脚本并传入 `--source-export-json`，生成 V45 单日日报。",
            "- 运行 V45 导入脚本，把单日日报写入台账并刷新 V42/V44。",
            "- 连续至少 90 个自然日且至少 60 个日报交易日后，再评估是否解除 V42 日报数量阻塞。",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    template_path = Path(args.template_json)
    example_path = Path(args.example_json)
    mapping_path = Path(args.mapping_json)
    _write_json(template_path, build_template())
    _write_json(example_path, build_example())
    _write_json(mapping_path, build_mapping())
    daily_output_path: Path | None = None
    if args.source_export_json:
        source = json.loads(Path(args.source_export_json).read_text(encoding="utf-8"))
        if not isinstance(source, dict):
            raise RuntimeError("source export must be a JSON object")
        daily_output_path = Path(args.daily_output_json)
        _write_json(daily_output_path, build_daily_report_from_export(source))
    _write_report(
        Path(args.report_md),
        template=template_path,
        example=example_path,
        mapping=mapping_path,
        daily_output=daily_output_path,
    )
    print(
        json.dumps(
            {
                "production_ready": False,
                "template": str(template_path),
                "example": str(example_path),
                "mapping": str(mapping_path),
                "daily_output": str(daily_output_path) if daily_output_path else "",
                "report": str(Path(args.report_md)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
