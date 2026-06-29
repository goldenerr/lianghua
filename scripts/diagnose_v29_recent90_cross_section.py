#!/usr/bin/env python3
"""Diagnose V29 May-2026 stock-sleeve cross-section attribution.

This is a research-only root-cause diagnostic for the recent-90 weakness. It
replays the V29 stock sleeves with V31 free-source trading-status constraints and
attributes the target month by selected symbol, industry, score bucket and a
60-day traded-amount liquidity proxy. It does not tune or change strategy
parameters.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from _paths import PROJECT_DIR, RESULTS_DIR
from research_v28_factor_direction import (
    PRICE_FACTOR_NAMES,
    _build_factors,
    _load_industry_map,
    _load_market_panel,
    _read_codes,
    _rolling_ic_weights,
    _score_ic_adaptive,
    _score_static,
    _select_weights,
)
from research_v29_portfolio_layer import (
    DEFAULT_V31_TRADING_STATUS,
    _apply_execution_constraints,
    _load_trading_status_constraints,
    _portfolio_configs,
    _sleeve_configs,
)

DEFAULT_OUTPUT = RESULTS_DIR / "quant_v29_recent90_cross_section_may2026.json"
DEFAULT_REPORT = Path("docs/research/quant_v29_recent90_cross_section_may2026.md")
DEFAULT_CANDIDATES = "v29_price_meta_longhorizon_guard,v29_price_meta_ultradefensive"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", default=DEFAULT_CANDIDATES)
    parser.add_argument("--universe", default=str(PROJECT_DIR / "data" / "stock_list.json"))
    parser.add_argument("--start-date", default="20060101")
    parser.add_argument("--end-date", default="20260625")
    parser.add_argument("--target-month", default="2026-05")
    parser.add_argument("--min-history", type=int, default=252)
    parser.add_argument("--trading-status-path", default=str(DEFAULT_V31_TRADING_STATUS))
    parser.add_argument("--require-trading-status", action="store_true", default=True)
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--report-md", default=str(DEFAULT_REPORT))
    return parser.parse_args()


def _safe_float(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def assign_quantile_buckets(
    values: pd.Series,
    *,
    labels: tuple[str, ...] = ("bottom", "low", "mid", "high", "top"),
) -> dict[str, str]:
    """Assign stable quantile buckets, preserving missing values as unknown."""
    numeric = pd.to_numeric(values, errors="coerce")
    out = {str(idx): "unknown" for idx in values.index}
    valid = numeric.dropna()
    if valid.empty:
        return out
    ranks = valid.rank(method="first", pct=True)
    n = len(labels)
    for idx, pct in ranks.items():
        bucket_idx = min(n - 1, max(0, int(math.ceil(float(pct) * n) - 1)))
        out[str(idx)] = labels[bucket_idx]
    return out


def group_contribution_summary(
    frame: pd.DataFrame, group_col: str, *, limit: int = 10
) -> list[dict[str, Any]]:
    if frame.empty or group_col not in frame.columns:
        return []
    rows: list[dict[str, Any]] = []
    for key, group in frame.groupby(group_col, dropna=False):
        stock_return = (
            group["stock_return"]
            if "stock_return" in group.columns
            else pd.Series(0.0, index=group.index)
        )
        weight = group["weight"] if "weight" in group.columns else pd.Series(0.0, index=group.index)
        symbol = group["symbol"] if "symbol" in group.columns else pd.Series([], dtype=object)
        rows.append(
            {
                group_col: "unknown" if pd.isna(key) else str(key),
                "contribution_sum": round(float(group["contribution"].sum()), 6),
                "avg_return": round(float(stock_return.mean()), 6),
                "avg_weight": round(float(weight.mean()), 6),
                "symbol_count": int(symbol.nunique()),
                "row_count": int(len(group)),
            }
        )
    rows.sort(key=lambda row: (row["contribution_sum"], -row["row_count"]))
    return rows[:limit]


def _score_for_sleeve(
    config: dict[str, Any],
    factors: dict[str, pd.DataFrame],
    rolling_ic: pd.DataFrame,
    t: int,
) -> pd.Series:
    if config["mode"] == "ic_adaptive":
        factor_names = None if config.get("factor_set") == "all" else PRICE_FACTOR_NAMES
        return _score_ic_adaptive(factors, rolling_ic, t, factor_names=factor_names)
    return _score_static(factors, t, config["factor_weights"])


def _factor_values_for_symbol(
    symbol: str,
    t: int,
    factors: dict[str, pd.DataFrame],
    config: dict[str, Any],
) -> dict[str, float]:
    if config["mode"] == "static":
        names = list(config.get("factor_weights", {}).keys())
    else:
        names = sorted(PRICE_FACTOR_NAMES if config.get("factor_set") != "all" else factors.keys())
    out: dict[str, float] = {}
    for name in names:
        frame = factors.get(name)
        if frame is None or symbol not in frame.columns or t >= len(frame):
            continue
        value = _safe_float(frame.iloc[t].get(symbol))
        if math.isfinite(value):
            out[name] = round(value, 8)
    return out


def trace_stock_sleeve_cross_section(
    *,
    sleeve_name: str,
    config: dict[str, Any],
    close: pd.DataFrame,
    amount: pd.DataFrame,
    daily_stock_returns: pd.DataFrame,
    factors: dict[str, pd.DataFrame],
    rolling_ic: pd.DataFrame,
    industry_map: dict[str, str],
    target_start: pd.Timestamp,
    target_end: pd.Timestamp,
    trading_constraints: dict[str, pd.DataFrame] | None,
) -> pd.DataFrame:
    positions: dict[str, float] = {}
    rows: list[dict[str, Any]] = []
    start_idx = 252
    latest_score = pd.Series(dtype=float)
    latest_score_buckets: dict[str, str] = {}
    latest_liquidity_buckets: dict[str, str] = {}
    latest_rebalance_date: pd.Timestamp | None = None
    for t in range(start_idx, len(close) - 1):
        if (t - start_idx) % int(config["rebalance_freq"]) == 0:
            score = _score_for_sleeve(config, factors, rolling_ic, t)
            new_positions = _select_weights(
                score,
                industry_map,
                top_n=int(config["top_n"]),
                max_per_industry=int(config["max_per_industry"]),
                gross=1.0,
            )
            if trading_constraints is not None:
                trade_date = pd.Timestamp(close.index[t + 1])
                new_positions, _diag = _apply_execution_constraints(
                    old_positions=positions,
                    target_positions=new_positions,
                    is_tradable=trading_constraints["is_tradable"].loc[trade_date],
                    is_limit_up=trading_constraints["is_limit_up"].loc[trade_date],
                    is_limit_down=trading_constraints["is_limit_down"].loc[trade_date],
                )
            positions = new_positions
            latest_score = score
            selected_score = score.reindex(list(positions)).dropna()
            latest_score_buckets = assign_quantile_buckets(
                selected_score, labels=("selected_low", "selected_mid", "selected_top")
            )
            liquidity = amount.iloc[max(0, t - 60) : t + 1].mean().reindex(list(positions))
            latest_liquidity_buckets = assign_quantile_buckets(
                liquidity, labels=("low_amount_proxy", "mid_amount_proxy", "high_amount_proxy")
            )
            latest_rebalance_date = pd.Timestamp(close.index[t + 1])
        date = pd.Timestamp(close.index[t + 1])
        if date < target_start or date > target_end:
            continue
        next_returns = daily_stock_returns.iloc[t + 1]
        for symbol, weight in positions.items():
            stock_return = _safe_float(next_returns.get(symbol))
            contribution = float(weight) * stock_return
            rows.append(
                {
                    "sleeve": sleeve_name,
                    "date": date.date().isoformat(),
                    "rebalance_date": (
                        latest_rebalance_date.date().isoformat()
                        if latest_rebalance_date is not None
                        else None
                    ),
                    "symbol": str(symbol),
                    "industry": industry_map.get(str(symbol), "__UNKNOWN__"),
                    "weight": float(weight),
                    "stock_return": stock_return,
                    "contribution": contribution,
                    "score": _safe_float(latest_score.get(symbol)),
                    "score_bucket": latest_score_buckets.get(str(symbol), "unknown"),
                    "liquidity_bucket": latest_liquidity_buckets.get(str(symbol), "unknown"),
                    "factor_values": _factor_values_for_symbol(str(symbol), t, factors, config),
                }
            )
    return pd.DataFrame(rows)


def build_candidate_cross_section_report(candidate: str, rows: pd.DataFrame) -> dict[str, Any]:
    if rows.empty:
        return {
            "candidate": candidate,
            "production_ready": False,
            "not_parameter_tuning": True,
            "total_stock_contribution": 0.0,
            "row_count": 0,
            "symbol_count": 0,
            "warning": "no selected stock rows for target month",
            "next_required_diagnostics": [
                "Verify target-month data coverage before interpreting sleeve attribution."
            ],
        }
    work = rows.copy()
    total = float(work["contribution"].sum())
    worst_symbols = (
        work.groupby("symbol", as_index=False)
        .agg(
            contribution_sum=("contribution", "sum"),
            avg_return=("stock_return", "mean"),
            avg_weight=("weight", "mean"),
            industry=("industry", "last"),
            row_count=("symbol", "size"),
        )
        .sort_values("contribution_sum")
        .head(15)
    )
    return {
        "candidate": candidate,
        "production_ready": False,
        "not_parameter_tuning": True,
        "total_raw_stock_contribution": round(total, 6),
        "raw_unscaled_note": "Sum of per-sleeve selected-stock contributions before V29 meta allocation scaling; use for cross-sectional root-cause attribution, not portfolio PnL.",
        "row_count": int(len(work)),
        "symbol_count": int(work["symbol"].nunique()),
        "sleeve_count": int(work["sleeve"].nunique()),
        "worst_industries": group_contribution_summary(work, "industry", limit=10),
        "worst_liquidity_buckets": group_contribution_summary(work, "liquidity_bucket", limit=10),
        "worst_score_buckets": group_contribution_summary(work, "score_bucket", limit=10),
        "worst_sleeves": group_contribution_summary(work, "sleeve", limit=10),
        "worst_symbols": [
            {
                "symbol": str(row.symbol),
                "industry": str(row.industry),
                "contribution_sum": round(float(row.contribution_sum), 6),
                "avg_return": round(float(row.avg_return), 6),
                "avg_weight": round(float(row.avg_weight), 6),
                "row_count": int(row.row_count),
            }
            for row in worst_symbols.itertuples(index=False)
        ],
        "next_required_diagnostics": [
            "Compare worst industry/liquidity buckets against historical winning months before changing factor signs.",
            "Trace whether selected_low/selected_top score buckets both lose; if both lose, the issue is regime-wide rather than rank cutoff only.",
            "Use a true market-cap/PIT security-master vendor feed before making production market-cap claims; amount bucket here is only a liquidity proxy.",
        ],
    }


def _load_replay_inputs(
    args: argparse.Namespace,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    dict[str, pd.DataFrame] | None,
    dict[str, Any],
    dict[str, Any],
]:
    codes = _read_codes(Path(args.universe))
    close, volume, amount = _load_market_panel(codes, min_history=int(args.min_history))
    start = pd.Timestamp(datetime.strptime(args.start_date, "%Y%m%d")).normalize()
    end = pd.Timestamp(datetime.strptime(args.end_date, "%Y%m%d")).normalize()
    close = close.loc[(close.index >= start) & (close.index <= end)].copy()
    volume = volume.reindex(close.index)
    amount = amount.reindex(close.index)
    daily_stock_returns = close.pct_change(fill_method=None).fillna(0.0)
    factors = _build_factors(close, volume, amount)
    future_5d = close.shift(-5) / close - 1.0
    rolling_ic = _rolling_ic_weights(factors, future_5d)
    industry_map = _load_industry_map()
    trading_constraints = None
    trading_summary: dict[str, Any] = {"enabled": False}
    if str(args.trading_status_path).strip():
        trading_constraints, trading_summary = _load_trading_status_constraints(
            Path(args.trading_status_path), close.index, close.columns
        )
        if args.require_trading_status and trading_constraints is None:
            raise RuntimeError(f"required V31 trading status unavailable: {trading_summary}")
    return (
        close,
        amount,
        daily_stock_returns,
        rolling_ic,
        trading_constraints,
        factors,
        {
            "symbols_loaded": int(close.shape[1]),
            "dates": int(close.shape[0]),
            "industry_overlap": int(len(set(close.columns) & set(industry_map))),
            "trading_constraint_summary": trading_summary,
            "industry_map": industry_map,
        },
    )


def build_full_report(args: argparse.Namespace, generated_at: datetime) -> dict[str, Any]:
    close, amount, daily_returns, rolling_ic, trading_constraints, factors, state = (
        _load_replay_inputs(args)
    )
    target_start = pd.Timestamp(f"{args.target_month}-01")
    target_end = target_start + pd.offsets.MonthEnd(0)
    portfolio_configs = {cfg["name"]: cfg for cfg in _portfolio_configs()}
    sleeve_defs = _sleeve_configs()
    candidates = [item.strip() for item in str(args.candidates).split(",") if item.strip()]
    candidate_reports: list[dict[str, Any]] = []
    row_frames: list[pd.DataFrame] = []
    industry_map = state.pop("industry_map")
    for candidate in candidates:
        if candidate not in portfolio_configs:
            raise RuntimeError(f"unknown candidate: {candidate}")
        candidate_rows: list[pd.DataFrame] = []
        for sleeve_name in portfolio_configs[candidate]["sleeves"]:
            sleeve_rows = trace_stock_sleeve_cross_section(
                sleeve_name=sleeve_name,
                config=copy.deepcopy(sleeve_defs[sleeve_name]),
                close=close,
                amount=amount,
                daily_stock_returns=daily_returns,
                factors=factors,
                rolling_ic=rolling_ic,
                industry_map=industry_map,
                target_start=target_start,
                target_end=target_end,
                trading_constraints=trading_constraints,
            )
            if not sleeve_rows.empty:
                sleeve_rows.insert(0, "candidate", candidate)
                candidate_rows.append(sleeve_rows)
                row_frames.append(sleeve_rows)
        combined = (
            pd.concat(candidate_rows, ignore_index=True) if candidate_rows else pd.DataFrame()
        )
        candidate_reports.append(build_candidate_cross_section_report(candidate, combined))
    all_rows = pd.concat(row_frames, ignore_index=True) if row_frames else pd.DataFrame()
    return {
        "ts": generated_at.isoformat(),
        "version": "V29-recent90-cross-section-may2026",
        "research_only": True,
        "production_ready": False,
        "not_parameter_tuning": True,
        "universe": str(args.universe),
        "target_month": args.target_month,
        "coverage": state,
        "candidate_count": len(candidate_reports),
        "candidates": candidate_reports,
        "top_loss_records": _top_loss_records(all_rows, limit=25),
        "decision": {
            "can_change_strategy_now": False,
            "next_step_class": "cross-sectional root-cause diagnosis, not parameter tuning",
            "limitations": [
                "amount/liquidity bucket is not true market capitalization; production market-cap attribution requires vendor PIT security master.",
                "V31 trading-status constraints are free-source research approximations, not production broker/vendor evidence.",
            ],
        },
    }


def _top_loss_records(frame: pd.DataFrame, *, limit: int) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    cols = [
        "candidate",
        "sleeve",
        "date",
        "symbol",
        "industry",
        "liquidity_bucket",
        "score_bucket",
        "weight",
        "stock_return",
        "contribution",
    ]
    rows = frame.sort_values("contribution").head(limit)[cols]
    out = []
    for row in rows.to_dict(orient="records"):
        out.append(
            {
                **{k: row[k] for k in row if k not in {"weight", "stock_return", "contribution"}},
                "weight": round(_safe_float(row["weight"]), 6),
                "stock_return": round(_safe_float(row["stock_return"]), 6),
                "contribution": round(_safe_float(row["contribution"]), 6),
            }
        )
    return out


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# V29 recent-90 May-2026 股票袖子横截面归因",
        "",
        "状态：研究诊断；不调参；不解除生产 blocker。",
        "",
        f"- target_month: {report['target_month']}",
        f"- production_ready: {str(report['production_ready']).lower()}",
        f"- symbols_loaded: {report['coverage']['symbols_loaded']}",
        "",
    ]
    for candidate in report["candidates"]:
        lines.extend(
            [
                f"## {candidate['candidate']}",
                "",
                f"- total_raw_stock_contribution: {candidate['total_raw_stock_contribution']}",
                f"- note: {candidate['raw_unscaled_note']}",
                f"- symbol_count: {candidate['symbol_count']}",
                "- worst_sleeves: "
                + ", ".join(
                    f"{row['sleeve']}={row['contribution_sum']}"
                    for row in candidate.get("worst_sleeves", [])[:3]
                ),
                "- worst_industries: "
                + ", ".join(
                    f"{row['industry']}={row['contribution_sum']}"
                    for row in candidate.get("worst_industries", [])[:5]
                ),
                "- worst_liquidity_buckets: "
                + ", ".join(
                    f"{row['liquidity_bucket']}={row['contribution_sum']}"
                    for row in candidate.get("worst_liquidity_buckets", [])[:5]
                ),
                "",
            ]
        )
    lines.extend(
        [
            "## 限制",
            "",
            "- liquidity/amount bucket 只是 60 日成交额代理，不是真实市值。真实市值归因需要 vendor PIT security master。",
            "- V31 trading status 是免费源研究近似，不是生产 broker/vendor evidence。",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    report = build_full_report(args, datetime.now(timezone.utc))
    _write_json(Path(args.output_json), report)
    _write_markdown(Path(args.report_md), report)
    print(
        json.dumps(
            {
                "production_ready": False,
                "not_parameter_tuning": True,
                "target_month": args.target_month,
                "candidate_count": report["candidate_count"],
                "output": args.output_json,
                "report": args.report_md,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
