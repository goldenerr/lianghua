#!/usr/bin/env python3
"""Research V34: small-account ETF rotation with executable lots and fees.

V33 showed that a 50k/100k RMB account cannot directly execute the broad
non-large-cap stock selection portfolio. V34 tests a more suitable small-account
shape: low-frequency ETF rotation with 100-share lots, minimum commission,
slippage, and cash drag. This remains research-only and never approves live
trading.
"""

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
from _paths import PROJECT_DIR, RESULTS_DIR
from research_v29_portfolio_layer import _walk_forward_from_returns
from research_v33_small_account_execution import (
    CostConfig,
    _affordable_buy_shares,
    _metrics,
    _order_fee,
    _round_down_to_lot,
)

DEFAULT_BENCHMARK_DIR = PROJECT_DIR / "data" / "benchmarks"
DEFAULT_OUTPUT_JSON = RESULTS_DIR / "quant_v34_small_account_etf_rotation.json"
DEFAULT_OUTPUT_CSV = RESULTS_DIR / "quant_v34_small_account_etf_rotation.csv"
DEFAULT_ETF_SYMBOLS = (
    "510300",  # 沪深300 ETF
    "510500",  # 中证500 ETF
    "159915",  # 创业板 ETF
    "512100",  # 中证1000 ETF
    "588000",  # 科创50 ETF
    "518880",  # 黄金 ETF
    "511880",  # 货币 ETF
)
DEFENSIVE_SYMBOLS = ("518880", "511880")
CASH_PROXY = "511880"


@dataclass(frozen=True)
class EtfScenario:
    capital: float
    max_assets: int
    rebalance_freq: int
    rank_mode: Literal["momentum", "risk_adjusted", "defensive"]
    risk_profile: Literal["balanced", "defensive", "capital_guard", "ultra_guard"]
    lot_size: int = 100
    lookback: int = 120


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-dir", default=str(DEFAULT_BENCHMARK_DIR))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument("--symbols", default=",".join(DEFAULT_ETF_SYMBOLS))
    parser.add_argument("--capitals", default="50000,100000,200000")
    parser.add_argument("--max-assets", default="1,2,3")
    parser.add_argument("--rebalance-freqs", default="20,40,60")
    parser.add_argument("--rank-modes", default="momentum,risk_adjusted,defensive")
    parser.add_argument("--risk-profiles", default="balanced,defensive,capital_guard,ultra_guard")
    parser.add_argument("--start-date", default="20130101")
    parser.add_argument("--end-date", default="20260529")
    parser.add_argument("--commission-rate", type=float, default=0.00025)
    parser.add_argument("--min-commission", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=3.0)
    return parser.parse_args()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_float_list(raw: str) -> list[float]:
    values = [float(item.strip()) for item in raw.split(",") if item.strip()]
    if not values:
        raise ValueError("expected at least one numeric value")
    return values


def _parse_int_list(raw: str) -> list[int]:
    values = [int(item.strip()) for item in raw.split(",") if item.strip()]
    if not values:
        raise ValueError("expected at least one integer value")
    return values


def _parse_str_list(raw: str) -> list[str]:
    values = [item.strip() for item in raw.split(",") if item.strip()]
    if not values:
        raise ValueError("expected at least one string value")
    return values


def _load_etf_panel(benchmark_dir: Path, symbols: list[str]) -> tuple[pd.DataFrame, dict[str, Any]]:
    close: dict[str, pd.Series] = {}
    metadata: dict[str, Any] = {}
    for symbol in symbols:
        path = benchmark_dir / f"etf_{symbol}.parquet"
        if not path.exists():
            metadata[symbol] = {"exists": False, "path": str(path)}
            continue
        frame = pd.read_parquet(path)
        if "close" not in frame.columns:
            metadata[symbol] = {"exists": True, "path": str(path), "failure": "missing close"}
            continue
        series = pd.to_numeric(frame["close"], errors="coerce")
        series.index = pd.to_datetime(frame.index, errors="coerce").normalize()
        series = series.loc[series.index.notna()].sort_index().dropna()
        if series.empty:
            metadata[symbol] = {"exists": True, "path": str(path), "failure": "empty close"}
            continue
        close[symbol] = series
        metadata[symbol] = {
            "exists": True,
            "path": str(path),
            "sha256": _sha256_file(path),
            "rows": int(len(series)),
            "start": series.index.min().date().isoformat(),
            "end": series.index.max().date().isoformat(),
            "first_close": round(float(series.iloc[0]), 4),
            "last_close": round(float(series.iloc[-1]), 4),
            "one_lot_value_latest": round(float(series.iloc[-1]) * 100, 2),
        }
    if not close:
        raise RuntimeError("no ETF benchmark close data available")
    panel = pd.DataFrame(close).sort_index()
    return panel, metadata


def _score_assets(close: pd.DataFrame, signal_idx: int, scenario: EtfScenario) -> pd.Series:
    if signal_idx < scenario.lookback:
        return pd.Series(dtype=float)
    window = close.iloc[: signal_idx + 1]
    current = window.iloc[-1]
    mom_20 = current / window.shift(20).iloc[-1] - 1.0
    mom_60 = current / window.shift(60).iloc[-1] - 1.0
    mom_120 = current / window.shift(120).iloc[-1] - 1.0
    ret_20 = window.pct_change(fill_method=None).tail(60)
    vol_60 = ret_20.std(ddof=1) * np.sqrt(252)
    drawdown_60 = current / window.tail(60).max() - 1.0
    if scenario.rank_mode == "momentum":
        score = 0.55 * mom_60 + 0.35 * mom_120 + 0.10 * mom_20
    elif scenario.rank_mode == "risk_adjusted":
        score = 0.55 * mom_60 + 0.35 * mom_120 + 0.10 * mom_20 - 0.35 * vol_60
    elif scenario.rank_mode == "defensive":
        score = 0.45 * mom_60 + 0.25 * mom_120 - 0.30 * vol_60 + 0.20 * drawdown_60
        for symbol in DEFENSIVE_SYMBOLS:
            if symbol in score.index:
                score.loc[symbol] += 0.03
    else:
        raise ValueError(f"unknown rank mode: {scenario.rank_mode}")
    return score.replace([np.inf, -np.inf], np.nan).dropna()


def _risk_budget(score: pd.Series, scenario: EtfScenario) -> float:
    if score.empty:
        return 0.0
    best = float(score.max())
    equity_scores = score.drop(labels=[symbol for symbol in DEFENSIVE_SYMBOLS if symbol in score.index])
    equity_best = float(equity_scores.max()) if not equity_scores.empty else -1.0
    if scenario.risk_profile == "capital_guard":
        if equity_best < 0.02 or best < -0.02:
            return 0.0
        if equity_best < 0.06:
            return 0.35
        return 0.60
    if scenario.risk_profile == "ultra_guard":
        if equity_best < 0.04 or best < 0.00:
            return 0.0
        if equity_best < 0.10:
            return 0.22
        return 0.35
    if scenario.risk_profile == "defensive":
        if equity_best < 0.00 or best < -0.03:
            return 0.0
        if equity_best < 0.03:
            return 0.35
        return 0.62
    if equity_best < -0.03:
        return 0.0
    if equity_best < 0.02:
        return 0.62
    return 0.95


def _target_weights(score: pd.Series, scenario: EtfScenario) -> dict[str, float]:
    budget = _risk_budget(score, scenario)
    if budget <= 1e-12:
        return {CASH_PROXY: 1.0} if CASH_PROXY in score.index else {}
    ranked = score.sort_values(ascending=False)
    if scenario.risk_profile == "defensive":
        risky_ranked = ranked.drop(labels=[symbol for symbol in DEFENSIVE_SYMBOLS if symbol in ranked.index])
        selected = risky_ranked.head(scenario.max_assets).index.tolist()
        defensive_weight = 1.0 - budget
        weights = {symbol: budget / max(1, len(selected)) for symbol in selected}
        if defensive_weight > 1e-12 and CASH_PROXY in score.index:
            weights[CASH_PROXY] = weights.get(CASH_PROXY, 0.0) + defensive_weight
        return {symbol: float(weight) for symbol, weight in weights.items() if weight > 1e-12}
    selected = ranked.head(scenario.max_assets).index.tolist()
    weights = {symbol: budget / max(1, len(selected)) for symbol in selected}
    cash_weight = 1.0 - budget
    if cash_weight > 1e-12 and CASH_PROXY in score.index:
        weights[CASH_PROXY] = weights.get(CASH_PROXY, 0.0) + cash_weight
    return {symbol: float(weight) for symbol, weight in weights.items() if weight > 1e-12}


def _apply_capital_guard(
    target_weights: dict[str, float],
    *,
    score: pd.Series,
    nav: float,
    peak_nav: float,
    scenario: EtfScenario,
) -> dict[str, float]:
    if scenario.risk_profile not in {"capital_guard", "ultra_guard"} or peak_nav <= 0:
        return target_weights
    current_dd = nav / peak_nav - 1.0
    equity_scores = score.drop(labels=[symbol for symbol in DEFENSIVE_SYMBOLS if symbol in score.index])
    equity_best = float(equity_scores.max()) if not equity_scores.empty else -1.0
    if current_dd <= -0.12 and equity_best < 0.08:
        return {CASH_PROXY: 1.0} if CASH_PROXY in score.index else {}
    if scenario.risk_profile == "ultra_guard" and current_dd <= -0.06 and equity_best < 0.12:
        return {CASH_PROXY: 1.0} if CASH_PROXY in score.index else {}
    if scenario.risk_profile == "ultra_guard" and current_dd <= -0.04:
        risky_symbols = [symbol for symbol in target_weights if symbol not in DEFENSIVE_SYMBOLS]
        guarded: dict[str, float] = {}
        risky_scale = 0.20 if equity_best >= 0.12 else 0.05
        shifted = 0.0
        for symbol, weight in target_weights.items():
            if symbol in risky_symbols:
                new_weight = weight * risky_scale
                guarded[symbol] = new_weight
                shifted += weight - new_weight
            else:
                guarded[symbol] = weight
        if shifted > 0 and CASH_PROXY in score.index:
            guarded[CASH_PROXY] = guarded.get(CASH_PROXY, 0.0) + shifted
        total = sum(guarded.values())
        return {symbol: float(weight / total) for symbol, weight in guarded.items() if total > 0}
    if current_dd <= -0.08:
        risky_symbols = [symbol for symbol in target_weights if symbol not in DEFENSIVE_SYMBOLS]
        guarded: dict[str, float] = {}
        risky_scale = 0.35 if equity_best >= 0.08 else 0.15
        shifted = 0.0
        for symbol, weight in target_weights.items():
            if symbol in risky_symbols:
                new_weight = weight * risky_scale
                guarded[symbol] = new_weight
                shifted += weight - new_weight
            else:
                guarded[symbol] = weight
        if shifted > 0 and CASH_PROXY in score.index:
            guarded[CASH_PROXY] = guarded.get(CASH_PROXY, 0.0) + shifted
        total = sum(guarded.values())
        return {symbol: float(weight / total) for symbol, weight in guarded.items() if total > 0}
    return target_weights


def _portfolio_value(positions: dict[str, int], cash: float, prices: pd.Series) -> tuple[float, float]:
    asset_value = 0.0
    for symbol, shares in positions.items():
        price = float(prices.get(symbol, np.nan))
        if math.isfinite(price) and price > 0:
            asset_value += float(shares) * price
    return float(cash + asset_value), float(asset_value)


def _execute_rebalance(
    *,
    positions: dict[str, int],
    cash: float,
    target_weights: dict[str, float],
    nav: float,
    prices: pd.Series,
    scenario: EtfScenario,
    cost: CostConfig,
) -> tuple[dict[str, int], float, dict[str, float]]:
    diagnostics = {
        "orders": 0.0,
        "buy_orders": 0.0,
        "sell_orders": 0.0,
        "buy_notional": 0.0,
        "sell_notional": 0.0,
        "fees": 0.0,
        "lot_blocked_targets": 0.0,
        "cash_blocked_buys": 0.0,
        "missing_price_blocks": 0.0,
    }
    target_shares: dict[str, int] = {}
    for symbol, weight in target_weights.items():
        price = float(prices.get(symbol, np.nan))
        if not math.isfinite(price) or price <= 0:
            diagnostics["missing_price_blocks"] += 1.0
            continue
        shares = _round_down_to_lot(nav * weight / price, scenario.lot_size)
        if shares <= 0:
            diagnostics["lot_blocked_targets"] += 1.0
            continue
        target_shares[symbol] = shares

    updated = {symbol: int(shares) for symbol, shares in positions.items() if shares > 0}
    for symbol, current in list(updated.items()):
        desired = int(target_shares.get(symbol, 0))
        sell_shares = current - desired
        if sell_shares <= 0:
            continue
        price = float(prices.get(symbol, np.nan))
        if not math.isfinite(price) or price <= 0:
            diagnostics["missing_price_blocks"] += 1.0
            continue
        notional = sell_shares * price
        fee = _order_fee(notional, "sell", cost)
        cash += notional - fee
        diagnostics["orders"] += 1.0
        diagnostics["sell_orders"] += 1.0
        diagnostics["sell_notional"] += notional
        diagnostics["fees"] += fee
        remaining = current - sell_shares
        if remaining > 0:
            updated[symbol] = remaining
        else:
            updated.pop(symbol, None)

    for symbol, desired in target_shares.items():
        current = int(updated.get(symbol, 0))
        buy_shares = desired - current
        if buy_shares <= 0:
            continue
        price = float(prices.get(symbol, np.nan))
        if not math.isfinite(price) or price <= 0:
            diagnostics["missing_price_blocks"] += 1.0
            continue
        affordable = _affordable_buy_shares(
            cash=cash,
            price=price,
            desired_shares=buy_shares,
            lot_size=scenario.lot_size,
            cost=cost,
        )
        if affordable <= 0:
            diagnostics["cash_blocked_buys"] += 1.0
            continue
        notional = affordable * price
        fee = _order_fee(notional, "buy", cost)
        cash -= notional + fee
        updated[symbol] = current + affordable
        diagnostics["orders"] += 1.0
        diagnostics["buy_orders"] += 1.0
        diagnostics["buy_notional"] += notional
        diagnostics["fees"] += fee

    return {symbol: shares for symbol, shares in updated.items() if shares > 0}, float(cash), diagnostics


def _run_scenario(close: pd.DataFrame, scenario: EtfScenario, cost: CostConfig) -> dict[str, Any]:
    valuation_close = close.ffill()
    positions: dict[str, int] = {}
    cash = float(scenario.capital)
    prev_nav = float(scenario.capital)
    peak_nav = float(scenario.capital)
    returns: list[float] = []
    dates: list[pd.Timestamp] = []
    holdings_counts: list[int] = []
    exposure_values: list[float] = []
    cash_weights: list[float] = []
    target_weight_rows: list[dict[str, float]] = []
    diagnostics = {
        "orders": 0.0,
        "buy_orders": 0.0,
        "sell_orders": 0.0,
        "buy_notional": 0.0,
        "sell_notional": 0.0,
        "fees": 0.0,
        "lot_blocked_targets": 0.0,
        "cash_blocked_buys": 0.0,
        "missing_price_blocks": 0.0,
    }
    start_idx = max(scenario.lookback + 1, 130)
    for i in range(start_idx, len(close)):
        date = pd.Timestamp(close.index[i])
        nav_before, _asset_value_before = _portfolio_value(positions, cash, valuation_close.iloc[i])
        if nav_before <= 0:
            raise RuntimeError(f"NAV became non-positive on {date.date()}: {nav_before}")
        if (i - start_idx) % scenario.rebalance_freq == 0:
            score = _score_assets(close, i - 1, scenario)
            target_weights = _apply_capital_guard(
                _target_weights(score, scenario),
                score=score,
                nav=nav_before,
                peak_nav=peak_nav,
                scenario=scenario,
            )
            positions, cash, diag = _execute_rebalance(
                positions=positions,
                cash=cash,
                target_weights=target_weights,
                nav=nav_before,
                prices=close.iloc[i],
                scenario=scenario,
                cost=cost,
            )
            for key, value in diag.items():
                diagnostics[key] += float(value)
            target_weight_rows.append({symbol: round(float(weight), 6) for symbol, weight in target_weights.items()})
        nav_after, asset_value_after = _portfolio_value(positions, cash, valuation_close.iloc[i])
        returns.append(float(nav_after / prev_nav - 1.0))
        dates.append(date)
        prev_nav = nav_after
        peak_nav = max(peak_nav, nav_after)
        holdings_counts.append(len(positions))
        exposure_values.append(float(asset_value_after / nav_after) if nav_after > 0 else 0.0)
        cash_weights.append(float(cash / nav_after) if nav_after > 0 else 0.0)

    returns_series = pd.Series(returns, index=pd.DatetimeIndex(dates), dtype=float)
    full = _metrics(returns_series, final_nav=prev_nav, initial_capital=scenario.capital)
    full.update(
        {
            "avg_holdings": round(float(np.mean(holdings_counts)), 2) if holdings_counts else 0.0,
            "max_holdings": int(max(holdings_counts)) if holdings_counts else 0,
            "avg_asset_exposure": round(float(np.mean(exposure_values)), 4) if exposure_values else 0.0,
            "avg_cash_weight": round(float(np.mean(cash_weights)), 4) if cash_weights else 0.0,
            "total_fees": round(float(diagnostics["fees"]), 2),
            "fees_pct_initial_capital": round(float(diagnostics["fees"] / scenario.capital), 4),
            "turnover_pct_initial_capital": round(
                float((diagnostics["buy_notional"] + diagnostics["sell_notional"]) / scenario.capital),
                4,
            ),
            "orders": int(diagnostics["orders"]),
            "lot_blocked_targets": int(diagnostics["lot_blocked_targets"]),
            "cash_blocked_buys": int(diagnostics["cash_blocked_buys"]),
            "missing_price_blocks": int(diagnostics["missing_price_blocks"]),
        }
    )
    wf = _walk_forward_from_returns(returns_series) if len(returns_series) > 2100 else {}
    minimum_gate = (
        full.get("sharpe_ratio", -999) >= 1.2
        and full.get("max_drawdown", -999) >= -0.15
        and full.get("win_rate", 0.0) >= 0.40
    )
    executable_gate = (
        full.get("avg_holdings", 0.0) >= 0.80
        and full.get("avg_asset_exposure", 0.0) >= 0.50
        and full.get("cash_blocked_buys", 10**9) <= full.get("orders", 0) * 0.20 + 10
    )
    return {
        "name": (
            f"v34_etf_{scenario.risk_profile}_{scenario.rank_mode}_"
            f"{int(scenario.capital)}_{scenario.max_assets}asset_{scenario.rebalance_freq}d"
        ),
        "scenario": asdict(scenario),
        "cost": asdict(cost),
        "research_only": True,
        "production_ready": False,
        "small_account_minimum_gate_passes": bool(minimum_gate),
        "small_account_executable_gate_passes": bool(executable_gate),
        "full": full,
        "wf": wf,
        "allocation_tail": target_weight_rows[-5:],
        "production_blockers": [
            "ETF rotation is local research evidence only, not live-trading approval",
            "benchmark ETF files are local/free-provider data, not production vendor entitlement evidence",
            "requires broker paper fills, position reconciliation and external production evidence gate",
        ],
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "name",
        "capital",
        "risk_profile",
        "rank_mode",
        "max_assets",
        "rebalance_freq",
        "small_account_minimum_gate_passes",
        "small_account_executable_gate_passes",
        "sharpe_ratio",
        "annual_return",
        "annual_volatility",
        "max_drawdown",
        "win_rate",
        "total_return",
        "final_nav",
        "avg_holdings",
        "avg_asset_exposure",
        "avg_cash_weight",
        "total_fees",
        "fees_pct_initial_capital",
        "orders",
        "lot_blocked_targets",
        "cash_blocked_buys",
        "avg_oos_sharpe",
        "min_oos_sharpe",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            full = row["full"]
            wf = row.get("wf") or {}
            oos_values = [fold.get("oos") for fold in wf.get("folds", []) if fold.get("oos") is not None]
            scenario = row["scenario"]
            writer.writerow(
                {
                    "name": row["name"],
                    "capital": scenario["capital"],
                    "risk_profile": scenario["risk_profile"],
                    "rank_mode": scenario["rank_mode"],
                    "max_assets": scenario["max_assets"],
                    "rebalance_freq": scenario["rebalance_freq"],
                    "small_account_minimum_gate_passes": row["small_account_minimum_gate_passes"],
                    "small_account_executable_gate_passes": row["small_account_executable_gate_passes"],
                    "sharpe_ratio": full.get("sharpe_ratio"),
                    "annual_return": full.get("annual_return"),
                    "annual_volatility": full.get("annual_volatility"),
                    "max_drawdown": full.get("max_drawdown"),
                    "win_rate": full.get("win_rate"),
                    "total_return": full.get("total_return"),
                    "final_nav": full.get("final_nav"),
                    "avg_holdings": full.get("avg_holdings"),
                    "avg_asset_exposure": full.get("avg_asset_exposure"),
                    "avg_cash_weight": full.get("avg_cash_weight"),
                    "total_fees": full.get("total_fees"),
                    "fees_pct_initial_capital": full.get("fees_pct_initial_capital"),
                    "orders": full.get("orders"),
                    "lot_blocked_targets": full.get("lot_blocked_targets"),
                    "cash_blocked_buys": full.get("cash_blocked_buys"),
                    "avg_oos_sharpe": wf.get("avg_oos_sharpe"),
                    "min_oos_sharpe": round(float(min(oos_values)), 4) if oos_values else None,
                }
            )


def main() -> None:
    started = time.time()
    args = _parse_args()
    benchmark_dir = Path(args.benchmark_dir)
    output_json = Path(args.output_json)
    output_csv = Path(args.output_csv)
    symbols = _parse_str_list(args.symbols)
    capitals = _parse_float_list(args.capitals)
    max_assets = _parse_int_list(args.max_assets)
    rebalance_freqs = _parse_int_list(args.rebalance_freqs)
    rank_modes = _parse_str_list(args.rank_modes)
    risk_profiles = _parse_str_list(args.risk_profiles)
    invalid_modes = sorted(set(rank_modes) - {"momentum", "risk_adjusted", "defensive"})
    if invalid_modes:
        raise ValueError(f"unsupported rank modes: {invalid_modes}")
    invalid_profiles = sorted(
        set(risk_profiles) - {"balanced", "defensive", "capital_guard", "ultra_guard"}
    )
    if invalid_profiles:
        raise ValueError(f"unsupported risk profiles: {invalid_profiles}")
    cost = CostConfig(
        commission_rate=float(args.commission_rate),
        min_commission=float(args.min_commission),
        stamp_duty_rate=0.0,
        slippage_bps=float(args.slippage_bps),
    )
    close, metadata = _load_etf_panel(benchmark_dir, symbols)
    close = close.loc[pd.Timestamp(args.start_date) : pd.Timestamp(args.end_date)].copy()
    close = close.dropna(how="all")
    if len(close) < 600:
        raise RuntimeError("not enough ETF data for V34 validation")
    scenarios = [
        EtfScenario(
            capital=capital,
            max_assets=asset_count,
            rebalance_freq=freq,
            rank_mode=rank_mode,  # type: ignore[arg-type]
            risk_profile=risk_profile,  # type: ignore[arg-type]
        )
        for capital in capitals
        for asset_count in max_assets
        for freq in rebalance_freqs
        for rank_mode in rank_modes
        for risk_profile in risk_profiles
    ]
    rows = [_run_scenario(close, scenario, cost) for scenario in scenarios]
    rows.sort(
        key=lambda row: (
            bool(row["small_account_minimum_gate_passes"]),
            bool(row["small_account_executable_gate_passes"]),
            float(row["full"].get("sharpe_ratio", -999)),
            float(row["full"].get("annual_return", -999)),
        ),
        reverse=True,
    )
    best_by_capital: dict[str, Any] = {}
    for capital in capitals:
        capital_rows = [row for row in rows if float(row["scenario"]["capital"]) == float(capital)]
        if capital_rows:
            best_by_capital[str(int(capital))] = capital_rows[0]
    report = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V34-small-account-etf-rotation-v1",
        "research_only": True,
        "production_ready": False,
        "objective": "small-account ETF rotation after 100-share lots, minimum commission and cash drag",
        "benchmark_dir": str(benchmark_dir),
        "symbols_requested": symbols,
        "coverage": {
            "assets_loaded": int(close.shape[1]),
            "dates": int(len(close)),
            "start": pd.Timestamp(close.index.min()).date().isoformat(),
            "end": pd.Timestamp(close.index.max()).date().isoformat(),
            "asset_metadata": metadata,
        },
        "cost_model": asdict(cost),
        "scenario_count": int(len(rows)),
        "best_by_capital": best_by_capital,
        "results": rows,
        "production_blockers": [
            "research result does not approve live trading",
            "requires production ETF vendor entitlement, broker order/fill replay and 90-day paper trading",
            "requires external WORM/Secret/Approval/Position/Capacity/DR production evidence gate",
        ],
        "elapsed_seconds": round(time.time() - started, 2),
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(output_csv, rows)
    print(
        json.dumps(
            {
                "production_ready": report["production_ready"],
                "scenario_count": report["scenario_count"],
                "best_by_capital": {
                    capital: {
                        "name": row["name"],
                        "sharpe": row["full"].get("sharpe_ratio"),
                        "annual_return": row["full"].get("annual_return"),
                        "max_drawdown": row["full"].get("max_drawdown"),
                        "avg_holdings": row["full"].get("avg_holdings"),
                        "avg_asset_exposure": row["full"].get("avg_asset_exposure"),
                        "final_nav": row["full"].get("final_nav"),
                    }
                    for capital, row in best_by_capital.items()
                },
                "output_json": str(output_json),
                "output_csv": str(output_csv),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
