#!/usr/bin/env python3
"""Research V40: very-low-budget stock alpha over the V39 defensive basket."""

from __future__ import annotations

import argparse
import csv
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

import pandas as pd
from _paths import HEDGE_ASSETS_DIR, PROJECT_DIR, RESULTS_DIR
from research_v28_factor_direction import (
    DEFAULT_UNIVERSE,
    _build_factors,
    _build_market_regime_features,
    _load_industry_map,
    _load_market_panel,
    _read_codes,
    _rolling_ic_weights,
)
from research_v29_portfolio_layer import (
    DEFAULT_V31_TRADING_STATUS,
    _load_trading_status_constraints,
)
from research_v33_small_account_execution import CostConfig, ScenarioConfig
from research_v33_small_account_execution import _run_scenario as _run_stock_scenario
from research_v36_multi_asset_regime_budget import _load_multi_asset_panel
from research_v38_joint_account_crisis_alpha import (
    JointScenario,
    _compare_baseline_returns,
    _evaluate_improvement,
    _min_oos,
    _run_joint_scenario,
)
from research_v39_long_history_crisis_basket import (
    CRISIS_SYMBOLS,
    CrisisScenario,
    _asset_data_evidence,
    _crisis_target_weights,
)
from research_v39_long_history_crisis_basket import _run_scenario as _run_v39_scenario

DEFAULT_BENCHMARK_DIR = PROJECT_DIR / "data" / "benchmarks"
DEFAULT_OUTPUT_JSON = RESULTS_DIR / "quant_v40_stock_alpha_defensive_basket.json"
DEFAULT_OUTPUT_CSV = RESULTS_DIR / "quant_v40_stock_alpha_defensive_basket.csv"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-dir", default=str(DEFAULT_BENCHMARK_DIR))
    parser.add_argument("--hedge-asset-dir", default=str(HEDGE_ASSETS_DIR))
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    parser.add_argument("--trading-status-path", default=str(DEFAULT_V31_TRADING_STATUS))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument("--capitals", default="50000,100000,200000")
    parser.add_argument("--stock-alpha-caps", default="0.03,0.05,0.07")
    parser.add_argument("--crisis-caps", default="0.15,0.20")
    parser.add_argument("--stock-rebalance-freqs", default="40")
    parser.add_argument("--start-date", default="20130101")
    parser.add_argument("--end-date", default="20260529")
    parser.add_argument("--long-history-min-days", type=int, default=1800)
    parser.add_argument("--commission-rate", type=float, default=0.00025)
    parser.add_argument("--min-commission", type=float, default=5.0)
    parser.add_argument("--stock-stamp-duty-rate", type=float, default=0.0005)
    parser.add_argument("--stock-slippage-bps", type=float, default=5.0)
    parser.add_argument("--etf-slippage-bps", type=float, default=3.0)
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


def _baseline_key(capital: float, crisis_cap: float) -> str:
    return f"{int(capital)}:{crisis_cap:.4f}"


def _stock_execution_gate(row: dict[str, Any]) -> bool:
    full = row["full"]
    stock_fees = float(row["diagnostics"]["fees_by_group"].get("stock_alpha", 0.0))
    return bool(
        float(full.get("avg_stock_holdings", 0.0)) > 0.0
        and int(full.get("max_stock_holdings", 0)) > 0
        and stock_fees > 0.0
    )


def _evaluate_stock_increment(row: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    gates = _evaluate_improvement(row, baseline)
    execution_gate = _stock_execution_gate(row)
    gates["stock_execution_gate_passes"] = execution_gate
    gates["relative_improvement_gate_passes"] = bool(
        gates["relative_improvement_gate_passes"] and execution_gate
    )
    gates["production_minimum_gate_passes"] = bool(
        gates["production_minimum_gate_passes"] and execution_gate
    )
    return gates


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "name",
        "capital",
        "stock_alpha_cap",
        "crisis_cap",
        "stock_rebalance_freq",
        "relative_improvement_gate_passes",
        "production_minimum_gate_passes",
        "stock_execution_gate_passes",
        "annual_return",
        "sharpe_ratio",
        "max_drawdown",
        "avg_oos_sharpe",
        "min_oos_sharpe",
        "annual_return_delta",
        "avg_target_stock",
        "avg_target_crisis",
        "avg_stock_holdings",
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
                    "stock_alpha_cap": row["scenario"]["stock_alpha_cap"],
                    "crisis_cap": row["scenario"]["crisis_cap"],
                    "stock_rebalance_freq": row["scenario"]["stock_rebalance_freq"],
                    "relative_improvement_gate_passes": row["gates"][
                        "relative_improvement_gate_passes"
                    ],
                    "production_minimum_gate_passes": row["gates"][
                        "production_minimum_gate_passes"
                    ],
                    "stock_execution_gate_passes": row["gates"][
                        "stock_execution_gate_passes"
                    ],
                    "annual_return": row["full"]["annual_return"],
                    "sharpe_ratio": row["full"]["sharpe_ratio"],
                    "max_drawdown": row["full"]["max_drawdown"],
                    "avg_oos_sharpe": row["wf"].get("avg_oos_sharpe"),
                    "min_oos_sharpe": _min_oos(row["wf"]),
                    "annual_return_delta": row["gates"]["annual_return_delta"],
                    "avg_target_stock": row["full"]["avg_target_stock"],
                    "avg_target_crisis": row["full"]["avg_target_crisis"],
                    "avg_stock_holdings": row["full"]["avg_stock_holdings"],
                    "max_actual_risk_exposure": row["full"]["max_actual_risk_exposure"],
                }
            )


def main() -> None:
    started = time.time()
    args = _parse_args()
    capitals = _float_list(args.capitals)
    stock_caps = _float_list(args.stock_alpha_caps)
    crisis_caps = _float_list(args.crisis_caps)
    stock_rebalance_freqs = _int_list(args.stock_rebalance_freqs)
    if any(value <= 0.0 or value > 0.10 for value in stock_caps):
        raise ValueError("V40 stock caps must be in (0, 0.10]")
    if any(value <= 0.0 or value > 0.30 for value in crisis_caps):
        raise ValueError("V40 crisis caps must be in (0, 0.30]")
    if any(value <= 0 or value % 20 != 0 for value in stock_rebalance_freqs):
        raise ValueError("V40 stock rebalance frequencies must be positive multiples of 20")
    benchmark_dir = Path(args.benchmark_dir)
    hedge_dir = Path(args.hedge_asset_dir)
    universe_path = Path(args.universe)
    output_json = Path(args.output_json)
    output_csv = Path(args.output_csv)
    start = pd.Timestamp(args.start_date)
    end = pd.Timestamp(args.end_date)

    core_close, _core_metadata, core_roles = _load_multi_asset_panel(
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
    missing_crisis = [
        symbol
        for symbol in CRISIS_SYMBOLS
        if not expanded_metadata.get(symbol, {}).get("long_history_ready")
    ]
    if missing_crisis:
        raise RuntimeError(f"V40 long-history crisis assets unavailable: {missing_crisis}")
    core_close = core_close.loc[start:end].dropna(how="all")

    codes = _read_codes(universe_path)
    stock_close, stock_volume, stock_amount = _load_market_panel(codes, min_history=252)
    stock_close = stock_close.loc[start:end].copy()
    common_index = pd.DatetimeIndex(core_close.index).intersection(stock_close.index).sort_values()
    core_close = core_close.reindex(common_index)
    crisis_close = expanded_close.reindex(common_index)[list(CRISIS_SYMBOLS)]
    stock_close = stock_close.reindex(common_index)
    stock_volume = stock_volume.reindex(index=common_index, columns=stock_close.columns)
    stock_amount = stock_amount.reindex(index=common_index, columns=stock_close.columns)
    valuation_stock_close = stock_close.ffill()
    factors = _build_factors(stock_close, stock_volume, stock_amount)
    future_5d = stock_close.pct_change(5, fill_method=None).shift(-5)
    rolling_ic = _rolling_ic_weights(factors, future_5d)
    stock_regime = _build_market_regime_features(stock_close)
    industry_map = _load_industry_map()
    trading_constraints, trading_summary = _load_trading_status_constraints(
        Path(args.trading_status_path),
        stock_close.index,
        stock_close.columns,
    )
    if trading_constraints is None:
        raise RuntimeError("V40 requires V31 trading-status constraints")

    stock_cost = CostConfig(
        commission_rate=float(args.commission_rate),
        min_commission=float(args.min_commission),
        stamp_duty_rate=float(args.stock_stamp_duty_rate),
        slippage_bps=float(args.stock_slippage_bps),
    )
    etf_cost = CostConfig(
        commission_rate=float(args.commission_rate),
        min_commission=float(args.min_commission),
        stamp_duty_rate=0.0,
        slippage_bps=float(args.etf_slippage_bps),
    )
    crisis_allocator = partial(
        _crisis_target_weights,
        max_assets=2,
        allocation_mode="equal_pair",
    )

    gate_returns: dict[tuple[float, float, int], pd.Series] = {}
    for capital in capitals:
        for stock_cap in stock_caps:
            for freq in stock_rebalance_freqs:
                gate_result = _run_stock_scenario(
                    scenario=ScenarioConfig(
                        capital=capital * stock_cap,
                        max_positions=2,
                        rank_mode="blend",
                        gross_profile="small_balanced",
                        rebalance_freq=freq,
                        max_per_industry=1,
                        candidate_pool_multiplier=10,
                        per_position_budget_buffer=1.50,
                    ),
                    close=stock_close,
                    valuation_close=valuation_stock_close,
                    factors=factors,
                    rolling_ic=rolling_ic,
                    regime=stock_regime,
                    industry_map=industry_map,
                    trading_constraints=trading_constraints,
                    cost=stock_cost,
                    include_return_series=True,
                )
                gate_returns[(capital, stock_cap, freq)] = gate_result["_return_series"]

    baselines: dict[str, dict[str, Any]] = {}
    v39_parity: dict[str, dict[str, Any]] = {}
    for capital in capitals:
        for crisis_cap in crisis_caps:
            key = _baseline_key(capital, crisis_cap)
            baseline = _run_joint_scenario(
                scenario=JointScenario(
                    capital=capital,
                    stock_alpha_cap=0.0,
                    crisis_cap=crisis_cap,
                ),
                etf_close=core_close,
                etf_roles=core_roles,
                stock_close=stock_close,
                valuation_stock_close=valuation_stock_close,
                factors=factors,
                rolling_ic=rolling_ic,
                industry_map=industry_map,
                trading_constraints=trading_constraints,
                stock_gate_returns=pd.Series(0.0, index=common_index),
                stock_cost=stock_cost,
                etf_cost=etf_cost,
                crisis_close=crisis_close,
                crisis_symbols=set(CRISIS_SYMBOLS),
                crisis_allocator=crisis_allocator,
                align_stock_rebalance_to_etf=True,
                include_series=True,
            )
            reference = _run_v39_scenario(
                scenario=CrisisScenario(
                    capital=capital,
                    crisis_cap=crisis_cap,
                    crisis_max_assets=2,
                    allocation_mode="equal_pair",
                ),
                core_close=core_close,
                core_roles=core_roles,
                crisis_close=crisis_close,
                cost=etf_cost,
                include_series=True,
            )
            v39_parity[key] = _compare_baseline_returns(
                baseline.pop("_return_series"), reference.pop("_return_series")
            )
            baseline.pop("_attribution")
            baselines[key] = baseline

    rows: list[dict[str, Any]] = []
    for capital in capitals:
        for crisis_cap in crisis_caps:
            baseline = baselines[_baseline_key(capital, crisis_cap)]
            for stock_cap in stock_caps:
                for freq in stock_rebalance_freqs:
                    suffix = "" if freq == 40 else f"_{freq}d"
                    row = _run_joint_scenario(
                        scenario=JointScenario(
                            capital=capital,
                            stock_alpha_cap=stock_cap,
                            crisis_cap=crisis_cap,
                            stock_rebalance_freq=freq,
                        ),
                        etf_close=core_close,
                        etf_roles=core_roles,
                        stock_close=stock_close,
                        valuation_stock_close=valuation_stock_close,
                        factors=factors,
                        rolling_ic=rolling_ic,
                        industry_map=industry_map,
                        trading_constraints=trading_constraints,
                        stock_gate_returns=gate_returns[(capital, stock_cap, freq)],
                        stock_cost=stock_cost,
                        etf_cost=etf_cost,
                        crisis_close=crisis_close,
                        crisis_symbols=set(CRISIS_SYMBOLS),
                        crisis_allocator=crisis_allocator,
                        align_stock_rebalance_to_etf=True,
                    )
                    row["name"] = (
                        f"v40_{int(capital)}_stock{int(stock_cap * 100)}_"
                        f"crisis{int(crisis_cap * 100)}{suffix}"
                    )
                    row["baseline_full"] = baseline["full"]
                    row["baseline_wf"] = baseline["wf"]
                    row["gates"] = _evaluate_stock_increment(row, baseline)
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
    report = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V40-stock-alpha-defensive-basket-v1",
        "research_only": True,
        "production_ready": False,
        "objective": "incremental executable stock alpha over the same-capital V39 crisis baseline",
        "methodology": {
            "crisis_assets": list(CRISIS_SYMBOLS),
            "crisis_allocation": "two-asset gold/bond equal pair",
            "stock_alpha": "V32/V29-derived V33 blend, two positions, configurable rebalance",
            "stock_rebalance_freqs": stock_rebalance_freqs,
            "stock_rebalance_alignment": "aligned to V36 ETF rebalance calendar",
            "comparison": "same capital and same crisis cap, stock cap fixed at zero",
        },
        "coverage": {
            "stock_symbols": int(stock_close.shape[1]),
            "dates": int(len(common_index)),
            "start": common_index.min().date().isoformat(),
            "end": common_index.max().date().isoformat(),
            "crisis_asset_data_evidence": {
                symbol: _asset_data_evidence(expanded_metadata[symbol])
                for symbol in CRISIS_SYMBOLS
            },
        },
        "trading_constraint_summary": trading_summary,
        "cost_model": {"stock": asdict(stock_cost), "etf": asdict(etf_cost)},
        "scenario_count": len(rows),
        "stock_execution_pass_count": int(
            sum(1 for row in rows if row["gates"]["stock_execution_gate_passes"])
        ),
        "relative_improvement_pass_count": int(
            sum(1 for row in rows if row["gates"]["relative_improvement_gate_passes"])
        ),
        "production_minimum_pass_count": int(
            sum(1 for row in rows if row["gates"]["production_minimum_gate_passes"])
        ),
        "v39_baseline_parity_passes": bool(
            v39_parity and all(item["passes"] for item in v39_parity.values())
        ),
        "v39_baseline_parity": v39_parity,
        "baseline_by_capital_and_crisis_cap": baselines,
        "best_by_capital": best_by_capital,
        "results": rows,
        "production_blockers": [
            "V40 remains local research evidence and does not approve live trading",
            "stock alpha must improve the same-capital same-crisis V39 baseline under cost stress",
            "free-source qfq and V31 status are not approved vendor PIT evidence",
            "requires broker order/fill replay and approved 90-day paper trading",
            "requires external WORM/Secret/Approval/Provider/Position/Capacity/DR evidence gate",
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
                "stock_execution_pass_count": report["stock_execution_pass_count"],
                "relative_improvement_pass_count": report["relative_improvement_pass_count"],
                "production_minimum_pass_count": report["production_minimum_pass_count"],
                "v39_baseline_parity_passes": report["v39_baseline_parity_passes"],
                "best_by_capital": {
                    capital: {
                        "name": row["name"],
                        "annual_return": row["full"]["annual_return"],
                        "sharpe": row["full"]["sharpe_ratio"],
                        "max_drawdown": row["full"]["max_drawdown"],
                        "avg_oos": row["wf"].get("avg_oos_sharpe"),
                        "min_oos": _min_oos(row["wf"]),
                        "annual_return_delta": row["gates"]["annual_return_delta"],
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
