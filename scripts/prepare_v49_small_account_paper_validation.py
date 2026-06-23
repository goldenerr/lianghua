#!/usr/bin/env python3
"""Prepare a fail-closed RMB 50k/100k paper-validation package."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _paths import RESULTS_DIR

DEFAULT_V33_RESEARCH = RESULTS_DIR / "quant_v33_small_account_execution.json"
DEFAULT_OUTPUT = RESULTS_DIR / "quant_v49_small_account_paper_validation_package.json"
DEFAULT_LEDGER = RESULTS_DIR / "quant_v49_small_account_paper_ledger.json"
DEFAULT_DAILY_TEMPLATE = RESULTS_DIR / "quant_v49_small_account_daily_report_template.json"
DEFAULT_REPORT = Path("docs/research/quant_v49_small_account_paper_validation.md")
DEFAULT_CAPITALS = (50000.0, 100000.0)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v33-research-json", default=str(DEFAULT_V33_RESEARCH))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--ledger-json", default=str(DEFAULT_LEDGER))
    parser.add_argument("--daily-template-json", default=str(DEFAULT_DAILY_TEMPLATE))
    parser.add_argument("--report-md", default=str(DEFAULT_REPORT))
    parser.add_argument("--capitals", default="50000,100000")
    parser.add_argument("--require-paper-validation-ready", action="store_true")
    return parser.parse_args()


def _load_json_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"JSON must be an object: {path}")
    return data


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_capitals(raw: str) -> tuple[float, ...]:
    values = tuple(float(item.strip()) for item in raw.split(",") if item.strip())
    if not values:
        raise ValueError("capitals must not be empty")
    if any(value <= 0 or not math.isfinite(value) for value in values):
        raise ValueError("capitals must be positive finite numbers")
    return values


def _float_metric(row: dict[str, Any], key: str, default: float) -> float:
    try:
        value = float(row.get("full", {}).get(key, default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _candidate_sort_key(row: dict[str, Any]) -> tuple[bool, bool, float, float, float, float]:
    return (
        bool(row.get("small_account_minimum_gate_passes")),
        bool(row.get("small_account_executable_gate_passes")),
        _float_metric(row, "sharpe_ratio", -999.0),
        _float_metric(row, "annual_return", -999.0),
        _float_metric(row, "max_drawdown", -999.0),
        -_float_metric(row, "fees_pct_initial_capital", 999.0),
    )


def _wf_summary(row: dict[str, Any]) -> dict[str, Any]:
    wf = row.get("wf") if isinstance(row.get("wf"), dict) else {}
    folds = wf.get("folds", []) if isinstance(wf, dict) else []
    oos_values = [
        float(fold["oos"])
        for fold in folds
        if isinstance(fold, dict)
        and fold.get("oos") is not None
        and math.isfinite(float(fold.get("oos")))
    ]
    return {
        "avg_oos_sharpe": wf.get("avg_oos_sharpe"),
        "min_oos_sharpe": round(min(oos_values), 4) if oos_values else None,
        "fold_count": len(folds) if isinstance(folds, list) else 0,
    }


def _paper_controls() -> dict[str, Any]:
    return {
        "paper_days_required": 90,
        "min_report_days": 60,
        "auto_trade_enabled": False,
        "live_order_submission_allowed": False,
        "max_live_capital_fraction": 0.0,
        "manual_approval_required_for_parameter_change": True,
        "daily_audit_hash_required": True,
        "broker_position_snapshot_required": True,
        "broker_order_fill_log_required": True,
        "weekly_backtest_vs_paper_drift_required": True,
        "max_abs_cumulative_return_drift": 0.05,
        "max_mean_abs_daily_return_drift_bps": 5.0,
        "max_slippage_over_expected_bps": 5.0,
        "max_cost_over_expected_bps": 5.0,
    }


def _production_blockers(row: dict[str, Any]) -> list[str]:
    blockers = [
        "5-10 万账户当前只允许 paper/pre-production validation，不允许实盘生产。",
        "缺真实 PIT/vendor corporate action、真实交易状态和真实 tick/minute 执行数据复核。",
        "缺 broker/provider/WORM/approval/position/borrow/capacity/DR 外部 evidence refs。",
        "缺连续 90 天 paper trading 日报、订单成交日志、持仓快照和审计归档。",
    ]
    if row.get("small_account_minimum_gate_passes") is not True:
        blockers.append("小账户股票 alpha 回测未达到生产最低绩效门槛。")
    return blockers


def _build_candidate(row: dict[str, Any], capital: float) -> dict[str, Any]:
    scenario = row.get("scenario", {})
    if not isinstance(scenario, dict):
        scenario = {}
    metrics = row.get("full", {})
    if not isinstance(metrics, dict):
        metrics = {}
    executable = bool(row.get("small_account_executable_gate_passes"))
    minimum = bool(row.get("small_account_minimum_gate_passes"))
    return {
        "account_id": f"paper_small_account_{int(capital)}",
        "capital": float(capital),
        "strategy_name": str(row.get("name", f"v33_small_account_{int(capital)}")),
        "stage": "paper_trading_preproduction_validation",
        "recommended_mode": "paper_orders_only_no_live_permission",
        "paper_start_allowed": executable,
        "production_candidate": bool(executable and minimum),
        "small_account_executable_gate_passes": executable,
        "small_account_minimum_gate_passes": minimum,
        "scenario": scenario,
        "cost_model": row.get("cost", {}),
        "backtest_metrics": metrics,
        "walk_forward_summary": _wf_summary(row),
        "controls": _paper_controls(),
        "risk_limits": {
            "max_single_symbol_notional_fraction": 0.20,
            "max_daily_loss_fraction": 0.02,
            "weekly_loss_reduce_fraction": 0.03,
            "drawdown_reduce_fraction": 0.10,
            "drawdown_stop_fraction": 0.15,
            "lot_size": int(scenario.get("lot_size", 100) or 100),
            "rebalance_freq_days": int(scenario.get("rebalance_freq", 10) or 10),
        },
        "production_blockers": _production_blockers(row),
    }


def select_candidates(
    v33_research: dict[str, Any],
    capitals: tuple[float, ...] = DEFAULT_CAPITALS,
) -> tuple[list[dict[str, Any]], list[str]]:
    rows = v33_research.get("results", [])
    if not isinstance(rows, list):
        return [], ["V33 results must be a list"]

    candidates: list[dict[str, Any]] = []
    blockers: list[str] = []
    for capital in capitals:
        capital_rows = [
            row
            for row in rows
            if isinstance(row, dict)
            and abs(float(row.get("scenario", {}).get("capital", -1.0)) - capital) <= 1e-9
        ]
        if not capital_rows:
            blockers.append(f"缺少 {int(capital)} 元 V33 小账户研究结果")
            continue
        ranked = sorted(capital_rows, key=_candidate_sort_key, reverse=True)
        chosen = ranked[0]
        candidate = _build_candidate(chosen, capital)
        if not candidate["paper_start_allowed"]:
            blockers.append(f"{int(capital)} 元候选未通过小账户可执行门禁")
        candidates.append(candidate)
    return candidates, blockers


def build_package(
    v33_research: dict[str, Any],
    capitals: tuple[float, ...] = DEFAULT_CAPITALS,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or datetime.now(timezone.utc)
    candidates, paper_start_blockers = select_candidates(v33_research, capitals)
    controls_closed = all(
        candidate["controls"]["auto_trade_enabled"] is False
        and candidate["controls"]["live_order_submission_allowed"] is False
        and candidate["controls"]["max_live_capital_fraction"] == 0.0
        for candidate in candidates
    )
    if not controls_closed:
        paper_start_blockers.append("paper controls must keep all live-trading permissions closed")
    paper_validation_ready = bool(candidates) and not paper_start_blockers and controls_closed
    production_candidates = [
        candidate for candidate in candidates if bool(candidate.get("production_candidate"))
    ]
    return {
        "ts": generated_at.isoformat(),
        "version": "V49-small-account-paper-validation-v1",
        "research_only": False,
        "production_ready": False,
        "small_live_ready": False,
        "paper_validation_ready": paper_validation_ready,
        "paper_evidence_ready": False,
        "shadow_candidate_exists": paper_validation_ready,
        "paper_shadow_ready": paper_validation_ready,
        "stage": "5-10万小账户 paper trading / 准生产验证",
        "capital_scope": [float(value) for value in capitals],
        "candidate_accounts": candidates,
        "candidate": {
            "auto_trade_enabled": False,
            "live_order_submission_allowed": False,
            "max_live_capital_fraction": 0.0,
            "account_count": len(candidates),
        },
        "blockers": paper_start_blockers,
        "paper_start_blockers": paper_start_blockers,
        "production_blockers": sorted(
            {
                blocker
                for candidate in candidates
                for blocker in candidate.get("production_blockers", [])
            }
        ),
        "summary": {
            "accounts_ready_for_paper_validation": len(candidates) if paper_validation_ready else 0,
            "production_candidate_count": len(production_candidates),
            "minimum_gate_pass_count": sum(
                1 for candidate in candidates if candidate["small_account_minimum_gate_passes"]
            ),
            "executable_gate_pass_count": sum(
                1 for candidate in candidates if candidate["small_account_executable_gate_passes"]
            ),
            "required_calendar_days": 90,
            "required_report_days": 60,
        },
        "source_files": {
            "v33_research": str(DEFAULT_V33_RESEARCH),
        },
        "next_actions": [
            "用只读行情和模拟成交账户每天生成 V49/V45 兼容日报。",
            "连续至少 90 个自然日、至少 60 个日报交易日后运行 V42 paper evidence gate。",
            "任何自动交易、实盘下单权限、风控违规、不变式违规或敏感信息都会阻塞准生产验证。",
            "即使 paper evidence 通过，也仍需 V30/V48/V44 外部生产证据后才能考虑小资金实盘。",
        ],
    }


def build_daily_template(package: dict[str, Any]) -> dict[str, Any]:
    account_ids = [candidate["account_id"] for candidate in package.get("candidate_accounts", [])]
    return {
        "template_only": True,
        "version": "V49-small-account-paper-daily-template",
        "说明": "这是 V49 小账户 paper 日报模板，不能作为真实证据入账；不要填写令牌、接口密钥、密码或私钥。",
        "account_ids": account_ids,
        "external_refs": {
            "paper_trading_90d": "paper://真实90天小账户paper验证引用",
        },
        "daily_report": {
            "account_id": account_ids[0] if account_ids else "paper_small_account_50000",
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
            "archive_ref": "worm://或archive://真实归档引用",
            "market_data_ref": "provider://或vendor://真实行情源引用",
            "position_snapshot_ref": "broker://或exchange://真实持仓快照引用",
            "order_fill_log_ref": "broker://或exchange://真实订单成交日志引用",
            "auto_trade_enabled": False,
            "live_order_submission_allowed": False,
        },
    }


def build_or_update_ledger(package: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing or {}
    daily_reports = existing.get("daily_reports", [])
    external_refs = existing.get("external_refs", {})
    if not isinstance(daily_reports, list):
        daily_reports = []
    if not isinstance(external_refs, dict):
        external_refs = {}
    return {
        "version": "V49-small-account-paper-ledger",
        "generated_by": "scripts/prepare_v49_small_account_paper_validation.py",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "stage": package["stage"],
        "production_ready": False,
        "paper_validation_ready": package["paper_validation_ready"],
        "candidate_accounts": package["candidate_accounts"],
        "external_refs": external_refs,
        "daily_reports": daily_reports,
    }


def _format_pct(value: Any) -> str:
    try:
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return "无"


def _write_report(path: Path, package: dict[str, Any], ledger_path: Path, template_path: Path) -> None:
    lines = [
        "# V49 5-10万小账户 Paper Trading / 准生产验证",
        "",
        "状态：已进入小账户 paper validation 准备阶段，但不代表生产批准。",
        "",
        "## 结论",
        "",
        f"- `paper_validation_ready={str(package['paper_validation_ready']).lower()}`",
        "- `paper_evidence_ready=false`",
        "- `production_ready=false`",
        "- 自动交易：关闭",
        "- 实盘下单：禁止",
        "- 实盘资金占用：0",
        "",
        "## 候选账户",
        "",
    ]
    for candidate in package["candidate_accounts"]:
        metrics = candidate["backtest_metrics"]
        lines.extend(
            [
                f"### {candidate['account_id']}",
                "",
                f"- 资金：{candidate['capital']:.0f} 元",
                f"- 策略：`{candidate['strategy_name']}`",
                f"- 年化收益：{_format_pct(metrics.get('annual_return'))}",
                f"- Sharpe：{metrics.get('sharpe_ratio')}",
                f"- 最大回撤：{_format_pct(metrics.get('max_drawdown'))}",
                f"- 胜率：{_format_pct(metrics.get('win_rate'))}",
                f"- 平均持仓数：{metrics.get('avg_holdings')}",
                f"- 平均股票暴露：{_format_pct(metrics.get('avg_stock_exposure'))}",
                f"- 小账户可执行门禁：{str(candidate['small_account_executable_gate_passes']).lower()}",
                f"- 生产最低绩效门禁：{str(candidate['small_account_minimum_gate_passes']).lower()}",
                "",
            ]
        )
    lines.extend(
        [
            "## 文件",
            "",
            f"- 日报台账：`{ledger_path}`",
            f"- 日报模板：`{template_path}`",
            "",
            "## 每日流程",
            "",
            "- 只读取真实行情，生成策略信号和模拟订单。",
            "- 模拟订单必须经过同一套风控、整数手、费用、滑点和不可成交状态约束。",
            "- 每天输出 V49/V45 兼容日报，包含收益、成本、滑点、审计 hash、持仓快照和订单成交日志 reference。",
            "- 导入日报后刷新 V42 paper evidence gate，连续 90 个自然日且至少 60 个报告日后再评估。",
            "",
            "## 生产阻塞项",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in package["production_blockers"])
    lines.extend(
        [
            "",
            "## 严禁事项",
            "",
            "- 严禁开启自动交易。",
            "- 严禁连接真实下单权限。",
            "- 严禁把模板、示例或本地 mock reference 当作真实证据。",
            "- 严禁在未完成 V30/V42/V48/V44 前把任何字段改成 `production_ready=true`。",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    capitals = _parse_capitals(args.capitals)
    research_path = Path(args.v33_research_json)
    package = build_package(_load_json_object(research_path), capitals)
    package["source_files"]["v33_research"] = str(research_path)

    output = Path(args.output_json)
    ledger_path = Path(args.ledger_json)
    template_path = Path(args.daily_template_json)
    existing_ledger = _load_json_object(ledger_path) if ledger_path.exists() else None
    ledger = build_or_update_ledger(package, existing_ledger)
    template = build_daily_template(package)

    _write_json(output, package)
    _write_json(ledger_path, ledger)
    _write_json(template_path, template)
    _write_report(Path(args.report_md), package, ledger_path, template_path)
    print(
        json.dumps(
            {
                "production_ready": False,
                "paper_validation_ready": package["paper_validation_ready"],
                "paper_evidence_ready": False,
                "account_count": len(package["candidate_accounts"]),
                "blocker_count": len(package["blockers"]),
                "output": str(output),
                "ledger": str(ledger_path),
                "daily_template": str(template_path),
                "report": str(Path(args.report_md)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.require_paper_validation_ready and not package["paper_validation_ready"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
