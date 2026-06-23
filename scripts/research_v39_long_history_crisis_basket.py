#!/usr/bin/env python3
"""Research V39: long-history gold and government-bond crisis basket."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
from _paths import HEDGE_ASSETS_DIR, PROJECT_DIR, RESULTS_DIR
from research_v29_portfolio_layer import _walk_forward_from_returns
from research_v33_small_account_execution import CostConfig, _metrics
from research_v36_multi_asset_regime_budget import (
    RegimeBudgetScenario,
    _coverage_summary,
    _load_multi_asset_panel,
    _market_regime,
)
from research_v36_multi_asset_regime_budget import _run_scenario as _run_v36_scenario
from research_v36_multi_asset_regime_budget import _score_assets as _score_core_assets
from research_v36_multi_asset_regime_budget import _target_weights as _target_core_weights
from research_v38_joint_account_crisis_alpha import (
    RISK_GATE_LIMIT,
    TARGET_RISK_CAPACITY,
    _compare_baseline_returns,
    _execute_joint_rebalance,
    _min_oos,
    _weakest_fold_attribution,
)

DEFAULT_BENCHMARK_DIR = PROJECT_DIR / "data" / "benchmarks"
DEFAULT_OUTPUT_JSON = RESULTS_DIR / "quant_v39_long_history_crisis_basket.json"
DEFAULT_OUTPUT_CSV = RESULTS_DIR / "quant_v39_long_history_crisis_basket.csv"
CRISIS_SYMBOLS = ("518880", "511010", "511260")
GOLD_SYMBOL = "518880"
BOND_SYMBOLS = {"511010", "511260"}
CASH_PROXY = "511880"


@dataclass(frozen=True)
class CrisisScenario:
    capital: float
    crisis_cap: float
    crisis_max_assets: int
    allocation_mode: Literal["equal_pair", "inverse_vol_pair"]
    rebalance_freq: int = 20
    lookback: int = 126
    lot_size: int = 100


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-dir", default=str(DEFAULT_BENCHMARK_DIR))
    parser.add_argument("--hedge-asset-dir", default=str(HEDGE_ASSETS_DIR))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument("--capitals", default="50000,100000,200000")
    parser.add_argument("--crisis-caps", default="0.10,0.15,0.20")
    parser.add_argument("--crisis-max-assets", default="1,2")
    parser.add_argument("--allocation-modes", default="equal_pair,inverse_vol_pair")
    parser.add_argument("--start-date", default="20130101")
    parser.add_argument("--end-date", default="20260529")
    parser.add_argument("--long-history-min-days", type=int, default=1800)
    parser.add_argument("--commission-rate", type=float, default=0.00025)
    parser.add_argument("--min-commission", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=3.0)
    return parser.parse_args()


def _float_list(raw: str) -> list[float]:
    values = [float(item.strip()) for item in raw.split(",") if item.strip()]
    if not values:
        raise ValueError("expected numeric values")
    return values


def _int_list(raw: str) -> list[int]:
    values = [int(item.strip()) for item in raw.split(",") if item.strip()]
    if not values:
        raise ValueError("expected integer values")
    return values


def _str_list(raw: str) -> list[str]:
    values = [item.strip() for item in raw.split(",") if item.strip()]
    if not values:
        raise ValueError("expected string values")
    return values


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _asset_data_evidence(metadata: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(metadata["path"]))
    frame = pd.read_parquet(path)
    requested_start = (
        str(frame["requested_start"].dropna().iloc[0])
        if "requested_start" in frame.columns and not frame["requested_start"].dropna().empty
        else None
    )
    requested_end = (
        str(frame["requested_end"].dropna().iloc[0])
        if "requested_end" in frame.columns and not frame["requested_end"].dropna().empty
        else None
    )
    source = (
        str(frame["source"].dropna().iloc[0])
        if "source" in frame.columns and not frame["source"].dropna().empty
        else None
    )
    return {
        "path": str(path),
        "sha256": _file_sha256(path),
        "rows": int(len(frame)),
        "start": pd.Timestamp(frame.index.min()).date().isoformat(),
        "end": pd.Timestamp(frame.index.max()).date().isoformat(),
        "source": source,
        "requested_start": requested_start,
        "requested_end": requested_end,
        "request_range_provenance": bool(requested_start and requested_end and source),
    }


def _crisis_score_table(close: pd.DataFrame, signal_idx: int) -> pd.DataFrame:
    columns = ["score", "volatility", "group"]
    if signal_idx < 120:
        return pd.DataFrame(columns=columns)
    rows: list[dict[str, Any]] = []
    for symbol in CRISIS_SYMBOLS:
        if symbol not in close.columns:
            continue
        series = pd.to_numeric(close[symbol].iloc[: signal_idx + 1], errors="coerce").dropna()
        if len(series) < 121:
            continue
        current = float(series.iloc[-1])
        momentum_60 = current / float(series.iloc[-61]) - 1.0
        momentum_120 = current / float(series.iloc[-121]) - 1.0
        above_ma120 = current > float(series.tail(120).mean())
        volatility = float(series.pct_change(fill_method=None).tail(60).std(ddof=1) * np.sqrt(252))
        if (
            momentum_60 <= 0.0
            or momentum_120 <= 0.0
            or not above_ma120
            or not math.isfinite(volatility)
            or volatility <= 1e-8
        ):
            continue
        trend = 0.40 * momentum_60 + 0.60 * momentum_120
        rows.append(
            {
                "symbol": symbol,
                "score": trend / max(volatility, 0.03),
                "volatility": volatility,
                "group": "gold" if symbol == GOLD_SYMBOL else "bond",
            }
        )
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows).set_index("symbol").sort_values("score", ascending=False)


def _select_diversified_crisis_assets(score: pd.DataFrame, max_assets: int) -> list[str]:
    if score.empty or max_assets <= 0:
        return []
    if max_assets == 1:
        return [str(score.index[0])]
    selected: list[str] = []
    for group in ("gold", "bond"):
        group_rows = score.loc[score["group"] == group]
        if not group_rows.empty:
            selected.append(str(group_rows.index[0]))
    for symbol in score.index:
        value = str(symbol)
        if value not in selected:
            selected.append(value)
        if len(selected) >= max_assets:
            break
    return selected[:max_assets]


def _crisis_target_weights(
    close: pd.DataFrame,
    *,
    signal_idx: int,
    regime: str,
    crisis_cap: float,
    max_assets: int,
    allocation_mode: str,
) -> tuple[dict[str, float], dict[str, Any]]:
    regime_scale = {"risk_off": 1.0, "cautious": 0.60, "neutral": 0.25, "risk_on": 0.0}.get(
        regime, 0.0
    )
    diagnostics: dict[str, Any] = {
        "regime": regime,
        "regime_scale": regime_scale,
        "selected": [],
        "target_budget": 0.0,
    }
    if crisis_cap <= 0.0 or regime_scale <= 0.0:
        return {}, diagnostics
    score = _crisis_score_table(close, signal_idx)
    selected = _select_diversified_crisis_assets(score, max_assets)
    if not selected:
        return {}, diagnostics
    budget = min(float(crisis_cap), float(crisis_cap) * regime_scale)
    if allocation_mode == "equal_pair":
        raw_weights = pd.Series(1.0, index=selected, dtype=float)
    elif allocation_mode == "inverse_vol_pair":
        raw_weights = 1.0 / score.loc[selected, "volatility"].clip(lower=0.03)
    else:
        raise ValueError(f"unsupported crisis allocation mode: {allocation_mode}")
    normalized = raw_weights / float(raw_weights.sum())
    weights = {str(symbol): float(budget * normalized.loc[symbol]) for symbol in selected}
    diagnostics.update(
        {
            "selected": selected,
            "target_budget": round(float(sum(weights.values())), 6),
            "scores": {symbol: round(float(score.loc[symbol, "score"]), 6) for symbol in selected},
        }
    )
    return weights, diagnostics


def _merge_targets(
    core_weights: dict[str, float], crisis_weights: dict[str, float]
) -> tuple[dict[str, float], dict[str, float]]:
    core = {
        str(symbol): max(0.0, float(weight))
        for symbol, weight in core_weights.items()
        if symbol != CASH_PROXY and float(weight) > 1e-12
    }
    core_total = float(sum(core.values()))
    if core_total > TARGET_RISK_CAPACITY:
        scale = TARGET_RISK_CAPACITY / core_total
        core = {symbol: weight * scale for symbol, weight in core.items()}
        core_total = TARGET_RISK_CAPACITY
    requested_crisis = float(sum(max(0.0, value) for value in crisis_weights.values()))
    crisis_total = min(requested_crisis, max(0.0, TARGET_RISK_CAPACITY - core_total))
    crisis_scale = crisis_total / requested_crisis if requested_crisis > 1e-12 else 0.0
    merged = dict(core)
    for symbol, weight in crisis_weights.items():
        if weight > 0.0 and crisis_scale > 0.0:
            merged[symbol] = merged.get(symbol, 0.0) + float(weight) * crisis_scale
    total = float(sum(merged.values()))
    if total > TARGET_RISK_CAPACITY + 1e-10:
        raise RuntimeError(f"V39 target capacity exceeded: {total}")
    return merged, {
        "core_target": round(core_total, 6),
        "crisis_target": round(crisis_total, 6),
        "total_target": round(total, 6),
    }


def _align_to_core_calendar(
    core_close: pd.DataFrame, crisis_close: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    core_index = pd.DatetimeIndex(core_close.index).sort_values()
    return core_close.reindex(core_index), crisis_close.reindex(core_index)


def _run_scenario(
    *,
    scenario: CrisisScenario,
    core_close: pd.DataFrame,
    core_roles: dict[str, str],
    crisis_close: pd.DataFrame,
    cost: CostConfig,
    include_series: bool = False,
) -> dict[str, Any]:
    core_close, crisis_close = _align_to_core_calendar(core_close, crisis_close)
    common_index = pd.DatetimeIndex(core_close.index)
    prices = core_close.copy()
    for symbol in CRISIS_SYMBOLS:
        if symbol in crisis_close.columns:
            prices[symbol] = crisis_close[symbol]
    valuation = prices.ffill()
    positions: dict[str, int] = {}
    cash = float(scenario.capital)
    prev_nav = float(scenario.capital)
    peak_nav = float(scenario.capital)
    returns: list[float] = []
    dates: list[pd.Timestamp] = []
    attribution_rows: list[dict[str, float]] = []
    exposures: list[float] = []
    crisis_targets: list[float] = []
    selected_counts: list[int] = []
    invariant_max_error = 0.0
    capacity_violations = 0
    diagnostics = {
        "orders": 0,
        "buy_orders": 0,
        "sell_orders": 0,
        "fees": 0.0,
        "fees_by_group": {"etf_core": 0.0, "crisis_gold": 0.0, "stock_alpha": 0.0},
        "cash_blocked_buys": 0,
        "lot_blocked_targets": 0,
        "missing_price_blocks": 0,
    }
    core_scenario = RegimeBudgetScenario(
        capital=scenario.capital,
        universe_mode="core_long",
        max_assets=1,
        rebalance_freq=scenario.rebalance_freq,
        rank_mode="risk_adjusted",
        risk_profile="aggressive",
    )
    start_idx = max(core_scenario.lookback + 1, 130)
    crisis_set = set(CRISIS_SYMBOLS)
    for i in range(start_idx, len(common_index)):
        date = pd.Timestamp(common_index[i])
        current_prices = valuation.iloc[i]
        previous_prices = valuation.iloc[i - 1]
        market_pnl = {"etf_core": 0.0, "crisis_basket": 0.0}
        for symbol, shares in positions.items():
            current = float(current_prices.get(symbol, np.nan))
            previous = float(previous_prices.get(symbol, np.nan))
            if not math.isfinite(current) or not math.isfinite(previous):
                continue
            group = "crisis_basket" if symbol in crisis_set else "etf_core"
            market_pnl[group] += shares * (current - previous)
        nav_before = prev_nav + sum(market_pnl.values())
        fee_by_group = {"etf_core": 0.0, "crisis_basket": 0.0}
        if (i - start_idx) % scenario.rebalance_freq == 0:
            signal_idx = i - 1
            regime, _ = _market_regime(core_close, signal_idx, core_roles)
            core_score = _score_core_assets(core_close, signal_idx, core_scenario, core_roles)
            core_weights, _ = _target_core_weights(
                core_close,
                signal_idx=signal_idx,
                score=core_score,
                scenario=core_scenario,
                nav=nav_before,
                peak_nav=peak_nav,
                roles=core_roles,
            )
            crisis_weights, crisis_diag = _crisis_target_weights(
                crisis_close,
                signal_idx=signal_idx,
                regime=regime,
                crisis_cap=scenario.crisis_cap,
                max_assets=scenario.crisis_max_assets,
                allocation_mode=scenario.allocation_mode,
            )
            target_weights, target_diag = _merge_targets(core_weights, crisis_weights)
            positions, cash, execution = _execute_joint_rebalance(
                positions=positions,
                cash=cash,
                target_weights=target_weights,
                nav=nav_before,
                prices=prices.iloc[i],
                trade_date=date,
                stock_symbols=set(),
                trading_constraints={},
                stock_cost=cost,
                etf_cost=cost,
                lot_size=scenario.lot_size,
                crisis_symbols=crisis_set,
            )
            for key in (
                "orders",
                "buy_orders",
                "sell_orders",
                "fees",
                "cash_blocked_buys",
                "lot_blocked_targets",
                "missing_price_blocks",
            ):
                diagnostics[key] += execution[key]
            for group, value in execution["fees_by_group"].items():
                diagnostics["fees_by_group"][group] += value
            fee_by_group["etf_core"] = execution["fees_by_group"]["etf_core"]
            fee_by_group["crisis_basket"] = execution["fees_by_group"]["crisis_gold"]
            crisis_targets.append(float(target_diag["crisis_target"]))
            selected_counts.append(len(crisis_diag["selected"]))
        nav_after = cash
        asset_value = 0.0
        for symbol, shares in positions.items():
            price = float(current_prices.get(symbol, np.nan))
            if math.isfinite(price) and price > 0.0:
                value = shares * price
                nav_after += value
                asset_value += value
        expected_nav = nav_before - sum(fee_by_group.values())
        invariant_error = abs(nav_after - expected_nav)
        invariant_max_error = max(invariant_max_error, invariant_error)
        if invariant_error > max(0.01, nav_after * 1e-8):
            raise RuntimeError(f"V39 equity invariant failed on {date.date()}: {invariant_error}")
        daily_return = nav_after / prev_nav - 1.0
        contribution = {
            group: (market_pnl[group] - fee_by_group[group]) / prev_nav for group in market_pnl
        }
        contribution["reconciliation_residual"] = daily_return - sum(contribution.values())
        exposure = asset_value / nav_after if nav_after > 0.0 else 0.0
        capacity_violations += int(exposure > RISK_GATE_LIMIT + 1e-9)
        returns.append(float(daily_return))
        dates.append(date)
        attribution_rows.append(contribution)
        exposures.append(float(exposure))
        prev_nav = nav_after
        peak_nav = max(peak_nav, nav_after)
    return_series = pd.Series(returns, index=pd.DatetimeIndex(dates), dtype=float, name="v39")
    attribution = pd.DataFrame(attribution_rows, index=return_series.index)
    full = _metrics(return_series, final_nav=prev_nav, initial_capital=scenario.capital)
    full.update(
        {
            "avg_actual_risk_exposure": round(float(np.mean(exposures)), 4),
            "max_actual_risk_exposure": round(float(max(exposures)), 4),
            "avg_crisis_target": round(float(np.mean(crisis_targets)), 4),
            "avg_selected_crisis_assets": round(float(np.mean(selected_counts)), 2),
            "orders": int(diagnostics["orders"]),
            "total_fees": round(float(diagnostics["fees"]), 2),
            "invariant_max_error": round(float(invariant_max_error), 10),
            "risk_capacity_violations": int(capacity_violations),
        }
    )
    wf = _walk_forward_from_returns(return_series) if len(return_series) > 2100 else {}
    result: dict[str, Any] = {
        "name": (
            f"v39_{int(scenario.capital)}_crisis{int(scenario.crisis_cap * 100)}_"
            f"{scenario.crisis_max_assets}asset_{scenario.allocation_mode}"
        ),
        "scenario": asdict(scenario),
        "research_only": True,
        "production_ready": False,
        "full": full,
        "wf": wf,
        "weakest_fold_attribution": _weakest_fold_attribution(return_series, attribution, wf),
        "diagnostics": diagnostics,
    }
    if include_series:
        result["_return_series"] = return_series
        result["_attribution"] = attribution
    return result


def _evaluate(row: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    full = row["full"]
    base = baseline["full"]
    min_oos = _min_oos(row["wf"])
    base_min_oos = _min_oos(baseline["wf"])
    annual_delta = float(full["annual_return"]) - float(base["annual_return"])
    sharpe_delta = float(full["sharpe_ratio"]) - float(base["sharpe_ratio"])
    drawdown_delta = float(full["max_drawdown"]) - float(base["max_drawdown"])
    oos_delta = float(row["wf"].get("avg_oos_sharpe", -999)) - float(
        baseline["wf"].get("avg_oos_sharpe", -999)
    )
    min_oos_delta = (
        float(min_oos) - float(base_min_oos)
        if min_oos is not None and base_min_oos is not None
        else -999.0
    )
    invariant_gate = (
        float(full["invariant_max_error"]) <= 0.01
        and int(full["risk_capacity_violations"]) == 0
        and float(full["max_actual_risk_exposure"]) <= RISK_GATE_LIMIT
    )
    relative = (
        annual_delta >= 0.005
        and sharpe_delta >= 0.0
        and float(full["max_drawdown"]) >= -0.15
        and drawdown_delta >= -0.02
        and oos_delta >= 0.0
        and min_oos_delta >= 0.0
        and invariant_gate
    )
    minimum = (
        float(full["sharpe_ratio"]) >= 1.2
        and float(full["max_drawdown"]) >= -0.15
        and float(full["win_rate"]) >= 0.40
        and min_oos is not None
        and min_oos >= 0.0
        and invariant_gate
    )
    return {
        "annual_return_delta": round(annual_delta, 4),
        "sharpe_delta": round(sharpe_delta, 4),
        "max_drawdown_delta": round(drawdown_delta, 4),
        "avg_oos_sharpe_delta": round(oos_delta, 4),
        "min_oos_sharpe_delta": round(min_oos_delta, 4),
        "joint_invariant_gate_passes": bool(invariant_gate),
        "relative_improvement_gate_passes": bool(relative),
        "production_minimum_gate_passes": bool(minimum),
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "name",
        "capital",
        "crisis_cap",
        "crisis_max_assets",
        "allocation_mode",
        "relative_improvement_gate_passes",
        "production_minimum_gate_passes",
        "annual_return",
        "sharpe_ratio",
        "max_drawdown",
        "avg_oos_sharpe",
        "min_oos_sharpe",
        "avg_crisis_target",
        "max_actual_risk_exposure",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "name": row["name"],
                    "capital": row["scenario"]["capital"],
                    "crisis_cap": row["scenario"]["crisis_cap"],
                    "crisis_max_assets": row["scenario"]["crisis_max_assets"],
                    "allocation_mode": row["scenario"]["allocation_mode"],
                    "relative_improvement_gate_passes": row["gates"][
                        "relative_improvement_gate_passes"
                    ],
                    "production_minimum_gate_passes": row["gates"][
                        "production_minimum_gate_passes"
                    ],
                    "annual_return": row["full"]["annual_return"],
                    "sharpe_ratio": row["full"]["sharpe_ratio"],
                    "max_drawdown": row["full"]["max_drawdown"],
                    "avg_oos_sharpe": row["wf"].get("avg_oos_sharpe"),
                    "min_oos_sharpe": _min_oos(row["wf"]),
                    "avg_crisis_target": row["full"]["avg_crisis_target"],
                    "max_actual_risk_exposure": row["full"]["max_actual_risk_exposure"],
                }
            )


def main() -> None:
    started = time.time()
    args = _parse_args()
    capitals = _float_list(args.capitals)
    crisis_caps = _float_list(args.crisis_caps)
    max_assets_values = _int_list(args.crisis_max_assets)
    allocation_modes = _str_list(args.allocation_modes)
    if any(value <= 0.0 or value > 0.30 for value in crisis_caps):
        raise ValueError("crisis caps must be in (0, 0.30]")
    if any(value not in {1, 2} for value in max_assets_values):
        raise ValueError("crisis max assets must be 1 or 2")
    invalid_modes = sorted(set(allocation_modes) - {"equal_pair", "inverse_vol_pair"})
    if invalid_modes:
        raise ValueError(f"unsupported allocation modes: {invalid_modes}")
    benchmark_dir = Path(args.benchmark_dir)
    hedge_dir = Path(args.hedge_asset_dir)
    output_json = Path(args.output_json)
    output_csv = Path(args.output_csv)
    core_close, core_metadata, core_roles = _load_multi_asset_panel(
        benchmark_dir,
        hedge_dir,
        "core_long",
        long_history_min_days=int(args.long_history_min_days),
    )
    expanded_close, expanded_metadata, _ = _load_multi_asset_panel(
        benchmark_dir,
        hedge_dir,
        "expanded_recent",
        long_history_min_days=int(args.long_history_min_days),
    )
    required_failures = [
        symbol
        for symbol in CRISIS_SYMBOLS
        if not expanded_metadata.get(symbol, {}).get("long_history_ready")
    ]
    if required_failures:
        raise RuntimeError(f"V39 long-history crisis assets unavailable: {required_failures}")
    start = pd.Timestamp(args.start_date)
    end = pd.Timestamp(args.end_date)
    core_close = core_close.loc[start:end].dropna(how="all")
    crisis_close = expanded_close.loc[start:end, list(CRISIS_SYMBOLS)].dropna(how="all")
    core_close, crisis_close = _align_to_core_calendar(core_close, crisis_close)
    common_index = pd.DatetimeIndex(core_close.index)
    cost = CostConfig(
        commission_rate=float(args.commission_rate),
        min_commission=float(args.min_commission),
        stamp_duty_rate=0.0,
        slippage_bps=float(args.slippage_bps),
    )
    core_coverage = _coverage_summary(
        core_metadata, long_history_min_days=int(args.long_history_min_days)
    )
    baselines: dict[float, dict[str, Any]] = {}
    parity: dict[str, dict[str, Any]] = {}
    for capital in capitals:
        baseline = _run_scenario(
            scenario=CrisisScenario(
                capital=capital,
                crisis_cap=0.0,
                crisis_max_assets=1,
                allocation_mode="equal_pair",
            ),
            core_close=core_close,
            core_roles=core_roles,
            crisis_close=crisis_close,
            cost=cost,
            include_series=True,
        )
        reference = _run_v36_scenario(
            core_close,
            RegimeBudgetScenario(
                capital=capital,
                universe_mode="core_long",
                max_assets=1,
                rebalance_freq=20,
                rank_mode="risk_adjusted",
                risk_profile="aggressive",
            ),
            cost,
            core_roles,
            core_coverage,
            include_return_series=True,
        )
        parity[str(int(capital))] = _compare_baseline_returns(
            baseline.pop("_return_series"), reference.pop("_return_series")
        )
        baseline.pop("_attribution")
        baselines[capital] = baseline
    rows: list[dict[str, Any]] = []
    for capital in capitals:
        for crisis_cap in crisis_caps:
            for max_assets in max_assets_values:
                for mode in allocation_modes:
                    row = _run_scenario(
                        scenario=CrisisScenario(
                            capital=capital,
                            crisis_cap=crisis_cap,
                            crisis_max_assets=max_assets,
                            allocation_mode=mode,  # type: ignore[arg-type]
                        ),
                        core_close=core_close,
                        core_roles=core_roles,
                        crisis_close=crisis_close,
                        cost=cost,
                    )
                    row["baseline_full"] = baselines[capital]["full"]
                    row["baseline_wf"] = baselines[capital]["wf"]
                    row["gates"] = _evaluate(row, baselines[capital])
                    rows.append(row)
    rows.sort(
        key=lambda row: (
            bool(row["gates"]["relative_improvement_gate_passes"]),
            float(row["gates"]["annual_return_delta"]),
            float(row["full"]["sharpe_ratio"]),
        ),
        reverse=True,
    )
    best_by_capital = {
        str(int(capital)): next(row for row in rows if row["scenario"]["capital"] == capital)
        for capital in capitals
    }
    futures_audit: dict[str, Any] = {}
    for path in sorted(hedge_dir.glob("fut_*.parquet")):
        frame = pd.read_parquet(path, columns=["close"])
        index = pd.to_datetime(frame.index, errors="coerce")
        futures_audit[path.stem] = {
            "rows": int(len(frame)),
            "start": index.min().date().isoformat(),
            "end": index.max().date().isoformat(),
            "research_only": True,
            "excluded_from_executable_returns": True,
        }
    report = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V39-long-history-crisis-basket-v1",
        "research_only": True,
        "production_ready": False,
        "objective": "independent long-history gold and government-bond crisis sleeve",
        "coverage": {
            "dates": int(len(common_index)),
            "start": common_index.min().date().isoformat(),
            "end": common_index.max().date().isoformat(),
            "crisis_assets": {
                symbol: expanded_metadata[symbol] for symbol in CRISIS_SYMBOLS
            },
            "crisis_asset_data_evidence": {
                symbol: _asset_data_evidence(expanded_metadata[symbol])
                for symbol in CRISIS_SYMBOLS
            },
            "futures_diagnostic": futures_audit,
        },
        "cost": asdict(cost),
        "scenario_count": len(rows),
        "relative_improvement_pass_count": int(
            sum(1 for row in rows if row["gates"]["relative_improvement_gate_passes"])
        ),
        "production_minimum_pass_count": int(
            sum(1 for row in rows if row["gates"]["production_minimum_gate_passes"])
        ),
        "v36_baseline_parity_passes": bool(parity and all(x["passes"] for x in parity.values())),
        "v36_baseline_parity_by_capital": parity,
        "baseline_by_capital": baselines,
        "best_by_capital": best_by_capital,
        "results": rows,
        "production_blockers": [
            "V39 remains local research evidence and does not approve live trading",
            "free-source qfq data is not approved vendor PIT corporate-action evidence",
            "gold benchmark history lacks request-range provenance present on the refreshed bond files",
            "futures are diagnostic only without broker margin, rollover and position evidence",
            "requires broker order/fill replay and approved 90-day paper trading",
            "requires external WORM/Secret/Approval/Provider/Capacity/DR evidence gate",
        ],
        "elapsed_seconds": round(time.time() - started, 2),
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(output_csv, rows)
    print(
        json.dumps(
            {
                "production_ready": False,
                "scenario_count": report["scenario_count"],
                "relative_improvement_pass_count": report["relative_improvement_pass_count"],
                "production_minimum_pass_count": report["production_minimum_pass_count"],
                "v36_baseline_parity_passes": report["v36_baseline_parity_passes"],
                "best_by_capital": {
                    capital: {
                        "name": row["name"],
                        "annual_return": row["full"]["annual_return"],
                        "sharpe": row["full"]["sharpe_ratio"],
                        "max_drawdown": row["full"]["max_drawdown"],
                        "avg_oos": row["wf"].get("avg_oos_sharpe"),
                        "min_oos": _min_oos(row["wf"]),
                    }
                    for capital, row in best_by_capital.items()
                },
                "output_json": str(output_json),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
