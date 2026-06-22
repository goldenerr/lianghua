#!/usr/bin/env python3
"""Research V16: multi-expert regime router.

Publicly observable lessons from top quant firms point toward many weak,
independent signals, disciplined data engineering and portfolio construction
rather than a single magic factor. This research-only script implements that
idea with the data already available locally:

- technical expert: rolling IC-weighted price/volume factors;
- fundamental expert: lagged quality/value/growth score;
- earnings expert: point-in-time announcement events from V15;
- defensive expert: low volatility, low idiosyncratic volatility, quality and
  lower price-impact characteristics.

The router changes expert weights by market regime, then keeps the existing
industry-relative, industry-rotation, hedge and defensive trend sleeves.
"""

from __future__ import annotations

import json
import os
import time
from copy import deepcopy
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
import research_dynamic_ic_overlay_v9 as v9
import research_earnings_events_v15 as v15
import research_industry_relative_v14 as v14
import validate_quant_logic_v5_9 as base
from _paths import RESULTS_DIR
from research_fundamental_quality_v7 import _load_fundamentals

RESULTS_DIR.mkdir(parents=True, exist_ok=True)

CONFIG: dict[str, float | int | str] = deepcopy(v15.CONFIG)
CONFIG.update(
    {
        "normal_technical_weight": 0.45,
        "normal_fundamental_weight": 0.35,
        "normal_earnings_weight": 0.10,
        "normal_defensive_weight": 0.10,
        "bear_technical_weight": 0.15,
        "bear_fundamental_weight": 0.35,
        "bear_earnings_weight": 0.20,
        "bear_defensive_weight": 0.30,
        "defensive_low_vol_weight": 0.35,
        "defensive_idio_vol_weight": 0.20,
        "defensive_quality_weight": 0.30,
        "defensive_liquidity_weight": 0.15,
        "min_expert_coverage": 80,
    }
)


def _quality_defensive_score(snapshot: pd.DataFrame) -> pd.Series:
    if snapshot.empty:
        return pd.Series(dtype=float)
    data = snapshot.copy()
    book = pd.to_numeric(data.get("book_value_per_share"), errors="coerce").replace(0, np.nan)
    cash = pd.to_numeric(data.get("operating_cash_flow_per_share"), errors="coerce")
    cashflow_quality = cash / book
    score = (
        0.35 * v15._event_z(data.get("roe", pd.Series(index=data.index, dtype=float))).fillna(0.0)
        + 0.25
        * v15._event_z(data.get("gross_margin", pd.Series(index=data.index, dtype=float))).fillna(0.0)
        - 0.25
        * v15._event_z(data.get("debt_asset_ratio", pd.Series(index=data.index, dtype=float))).fillna(0.0)
        + 0.15 * v15._event_z(cashflow_quality).fillna(0.0)
    )
    return v15._event_z(score)


def _defensive_expert_score(
    factors: dict[str, pd.DataFrame],
    local_t: int,
    symbols: list[str],
    snapshot: pd.DataFrame,
    cfg: dict[str, float | int | str],
) -> pd.Series:
    low_vol = factors["low_vol_60"].iloc[local_t].reindex(symbols)
    idio_vol = factors["idio_vol_60"].iloc[local_t].reindex(symbols)
    liquidity = factors["amihud_low"].iloc[local_t].reindex(symbols)
    quality = _quality_defensive_score(snapshot).reindex(symbols)
    raw = (
        float(cfg["defensive_low_vol_weight"]) * v15._event_z(low_vol).fillna(0.0)
        + float(cfg["defensive_idio_vol_weight"]) * v15._event_z(idio_vol).fillna(0.0)
        + float(cfg["defensive_quality_weight"]) * v15._event_z(quality).fillna(0.0)
        + float(cfg["defensive_liquidity_weight"]) * v15._event_z(liquidity).fillna(0.0)
    )
    return v15._event_z(raw).dropna()


def _router_weights(bear: bool, cfg: dict[str, float | int | str]) -> dict[str, float]:
    prefix = "bear" if bear else "normal"
    weights = {
        "technical": float(cfg[f"{prefix}_technical_weight"]),
        "fundamental": float(cfg[f"{prefix}_fundamental_weight"]),
        "earnings": float(cfg[f"{prefix}_earnings_weight"]),
        "defensive": float(cfg[f"{prefix}_defensive_weight"]),
    }
    total = sum(max(value, 0.0) for value in weights.values())
    if total <= 0:
        return {"technical": 0.5, "fundamental": 0.3, "earnings": 0.1, "defensive": 0.1}
    return {name: max(value, 0.0) / total for name, value in weights.items()}


def _combine_experts(
    technical: pd.Series,
    fundamental: pd.Series,
    earnings: pd.Series,
    defensive: pd.Series,
    symbols: list[str],
    bear: bool,
    cfg: dict[str, float | int | str],
) -> pd.Series:
    weights = _router_weights(bear, cfg)
    scores = {
        "technical": technical.reindex(symbols),
        "fundamental": fundamental.reindex(symbols),
        "earnings": earnings.reindex(symbols),
        "defensive": defensive.reindex(symbols),
    }
    combined = pd.Series(0.0, index=symbols, dtype=float)
    total = pd.Series(0.0, index=symbols, dtype=float)
    for name, score in scores.items():
        z = v15._event_z(score)
        valid = z.notna()
        if int(valid.sum()) < int(cfg["min_expert_coverage"]):
            continue
        combined.loc[valid] += weights[name] * z.loc[valid]
        total.loc[valid] += weights[name]
    result = (combined / total.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)
    return v15._event_z(result).dropna()


def _hedged_backtest(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    amount: pd.DataFrame,
    turnover: pd.DataFrame,
    fundamentals: dict[str, pd.DataFrame],
    industry_map: dict[str, str],
    earnings_events: pd.DataFrame,
    start_idx: int,
    end_idx: int,
    cfg: dict[str, float | int | str],
    *,
    warmup: int | None = None,
) -> dict[str, Any] | None:
    base_cfg: dict[str, float | int] = {
        key: value for key, value in cfg.items() if isinstance(value, int | float)
    }
    warmup = int(warmup or base_cfg["warmup_days"])
    if end_idx - start_idx < warmup + 30:
        return None
    close_slice = close.iloc[start_idx:end_idx]
    factors = v9._technical_factor_matrices(
        close_slice,
        volume.iloc[start_idx:end_idx],
        amount.iloc[start_idx:end_idx],
        turnover.iloc[start_idx:end_idx],
    )
    ic_history = v9._precompute_ic_history(factors, close_slice, int(base_cfg["ic_horizon"]))
    trend_prices = v14._load_trend_prices(close.index)
    trend_returns = trend_prices.pct_change(fill_method=None).fillna(0.0)
    hedge_returns = v14._load_hedge_returns(close.index, str(cfg["hedge_asset"]))
    positions: dict[str, float] = {}
    overlay_weights: dict[str, float] = {"cash": 0.0}
    hedge_weight = 0.0
    returns: list[float] = []
    turnovers: list[float] = []
    hedge_turnovers: list[float] = []
    trend_turnovers: list[float] = []
    active_counts: list[int] = []
    event_counts: list[int] = []
    bear_counts = 0
    equity = 1.0
    peak_equity = 1.0
    total_cost = 0.0
    exposure_values: list[float] = []
    event_cache: dict[pd.Timestamp, pd.Series] = {}
    for t in range(start_idx + warmup, end_idx - 1):
        local_t = t - start_idx
        date = pd.Timestamp(close.index[t])
        cost_today = 0.0
        if (t - start_idx - warmup) % int(base_cfg["rebalance_freq"]) == 0:
            latest = close.iloc[t].dropna()
            symbols = [
                symbol for symbol in latest.index if close.iloc[start_idx:t][symbol].count() >= 252
            ]
            if not symbols:
                new_positions: dict[str, float] = {}
                gross = 0.0
                bear = True
            else:
                close_window = close.iloc[max(start_idx, t - 252) : t + 1][symbols]
                gross, bear = v9._market_exposure(close_window, base_cfg)
                bear_counts += int(bear)
                tech_weights = v9._dynamic_ic_weights(ic_history, date, base_cfg)
                tech_score = pd.Series(0.0, index=symbols)
                tech_total = pd.Series(0.0, index=symbols)
                for name, weight in tech_weights.items():
                    z = v15._event_z(factors[name].iloc[local_t].reindex(symbols))
                    valid = z.notna()
                    tech_score.loc[valid] += weight * z.loc[valid]
                    tech_total.loc[valid] += weight
                tech_score = (tech_score / tech_total.replace(0, np.nan)).dropna()
                snapshot = v9._fundamental_snapshot(
                    fundamentals,
                    symbols,
                    date,
                    int(base_cfg["report_lag_days"]),
                )
                fund_score = v9._score(snapshot, latest)
                if date not in event_cache:
                    event_cache[date] = v15._earnings_event_score(earnings_events, date, cfg)
                event_score = event_cache[date].reindex(symbols)
                event_counts.append(int(event_score.notna().sum()))
                defensive_score = _defensive_expert_score(factors, local_t, symbols, snapshot, cfg)
                combined = _combine_experts(
                    tech_score,
                    fund_score,
                    event_score,
                    defensive_score,
                    symbols,
                    bear,
                    cfg,
                )
                combined = v14._industry_relative_blend(combined, industry_map, cfg)
                current_dd = (equity - peak_equity) / peak_equity
                if current_dd < -float(base_cfg["dd_stop_threshold"]):
                    gross *= float(base_cfg["dd_stop_scale"])
                elif current_dd < -float(base_cfg["dd_reduce_threshold"]):
                    gross *= float(base_cfg["dd_reduce_scale"])
                industry_scores = v14._industry_rotation_scores(close, industry_map, t, cfg)
                combined = v14._apply_industry_rotation(
                    combined,
                    industry_scores,
                    industry_map,
                    bear,
                    cfg,
                )
                if len(combined) < int(base_cfg["min_stocks"]):
                    gross *= float(cfg["industry_risk_off_scale"]) if bear else 0.5
                    selected = []
                else:
                    selected = v9._select(combined, industry_map, base_cfg)
                weight = (
                    min(gross / len(selected), float(base_cfg["max_position_pct"]))
                    if selected
                    else 0.0
                )
                new_positions = {symbol: weight for symbol in selected}
            residual = max(0.0, 1.0 - sum(new_positions.values()))
            new_hedge = -(
                float(cfg["bear_hedge_weight"]) if bear else float(cfg["normal_hedge_weight"])
            ) * sum(new_positions.values())
            new_overlay = v14._trend_overlay_weights(trend_prices, t, residual, bear, cfg)
            cost_today, turnover_today = v9._rebalance_cost(new_positions, positions, base_cfg)
            hedge_turnover = abs(new_hedge - hedge_weight)
            cost_today += hedge_turnover * float(cfg["hedge_cost"])
            trend_turnover = sum(
                abs(new_overlay.get(asset, 0.0) - overlay_weights.get(asset, 0.0))
                for asset in set(new_overlay) | set(overlay_weights)
            )
            cost_today += trend_turnover * float(cfg["trend_cost"])
            positions = new_positions
            overlay_weights = new_overlay
            hedge_weight = new_hedge
            turnovers.append(turnover_today)
            hedge_turnovers.append(hedge_turnover)
            trend_turnovers.append(trend_turnover)
            total_cost += cost_today
            active_counts.append(len(positions))
            exposure_values.append(sum(abs(weight) for weight in positions.values()) + abs(hedge_weight))
        day_return = -cost_today
        current = close.iloc[t]
        nxt = close.iloc[t + 1]
        for symbol, weight in positions.items():
            p0 = current.get(symbol, np.nan)
            p1 = nxt.get(symbol, np.nan)
            if pd.notna(p0) and pd.notna(p1) and p0 > 0 and p1 > 0:
                day_return += weight * (float(p1) / float(p0) - 1.0)
        overlay_day = trend_returns.iloc[t + 1]
        for asset, weight in overlay_weights.items():
            day_return += weight * float(overlay_day.get(asset, 0.0))
        day_return += hedge_weight * float(hedge_returns.iloc[t + 1])
        returns.append(day_return)
        equity *= 1.0 + day_return
        peak_equity = max(peak_equity, equity)
    if len(returns) < 50:
        return None
    arr = np.array(returns, dtype=float)
    ann_return = float(np.mean(arr) * 252)
    ann_vol = float(np.std(arr, ddof=1) * np.sqrt(252))
    sharpe = (ann_return - float(base_cfg["risk_free_rate"])) / ann_vol if ann_vol > 1e-8 else None
    curve = np.cumprod(1 + arr)
    peak = np.maximum.accumulate(curve)
    max_dd = float(np.min((curve - peak) / peak))
    return {
        "sharpe_ratio": round(float(sharpe), 4) if sharpe is not None else None,
        "annual_return": round(ann_return, 4),
        "annual_volatility": round(ann_vol, 4),
        "max_drawdown": round(max_dd, 4),
        "win_rate": round(float(np.mean(arr > 0)), 4),
        "total_return": round(float(curve[-1] - 1), 4),
        "avg_active_stocks": round(float(np.mean(active_counts)), 1) if active_counts else 0.0,
        "avg_event_symbols": round(float(np.mean(event_counts)), 1) if event_counts else 0.0,
        "avg_turnover": round(float(np.mean(turnovers)), 4) if turnovers else 0.0,
        "avg_hedge_turnover": round(float(np.mean(hedge_turnovers)), 4) if hedge_turnovers else 0.0,
        "avg_trend_turnover": round(float(np.mean(trend_turnovers)), 4) if trend_turnovers else 0.0,
        "avg_exposure": round(float(np.mean(exposure_values)), 4) if exposure_values else 0.0,
        "bear_rebalances": int(bear_counts),
        "total_cost": round(total_cost, 4),
        "n_days": len(arr),
    }


def _walk_forward(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    amount: pd.DataFrame,
    turnover: pd.DataFrame,
    fundamentals: dict[str, pd.DataFrame],
    industry_map: dict[str, str],
    earnings_events: pd.DataFrame,
    cfg: dict[str, float | int | str],
) -> dict[str, Any]:
    n = len(close)
    oos_total = int(n * float(cfg["oos_pct"]))
    fold_size = oos_total // int(cfg["n_folds"])
    first_test_start = n - oos_total
    purge = int(cfg["purge_days"])
    folds = []
    is_values = []
    oos_values = []
    for fold in range(int(cfg["n_folds"])):
        test_start = first_test_start + fold * fold_size
        test_end = min(test_start + fold_size, n)
        train_end = test_start - purge
        train = _hedged_backtest(
            close,
            volume,
            amount,
            turnover,
            fundamentals,
            industry_map,
            earnings_events,
            0,
            train_end,
            cfg,
        )
        context_start = max(0, test_start - int(cfg["warmup_days"]))
        oos = _hedged_backtest(
            close,
            volume,
            amount,
            turnover,
            fundamentals,
            industry_map,
            earnings_events,
            context_start,
            test_end,
            cfg,
            warmup=test_start - context_start,
        )
        row = {
            "fold": fold,
            "is": train["sharpe_ratio"] if train else None,
            "oos": oos["sharpe_ratio"] if oos else None,
            "mdd": oos["max_drawdown"] if oos else None,
        }
        if row["is"] is not None:
            is_values.append(float(row["is"]))
        if row["oos"] is not None:
            oos_values.append(float(row["oos"]))
        folds.append(row)
        print(f"  F{fold}: {row}", flush=True)
    avg_is = float(np.mean(is_values)) if is_values else np.nan
    avg_oos = float(np.mean(oos_values)) if oos_values else np.nan
    decay = (avg_is - avg_oos) / abs(avg_is) if abs(avg_is) > 1e-8 else np.nan
    return {
        "folds": folds,
        "avg_is_sharpe": round(avg_is, 4) if np.isfinite(avg_is) else None,
        "avg_oos_sharpe": round(avg_oos, 4) if np.isfinite(avg_oos) else None,
        "sharpe_decay": round(decay, 4) if np.isfinite(decay) else None,
    }


def _gates(full: dict[str, Any] | None, wf: dict[str, Any] | None) -> dict[str, bool]:
    result = {
        "S": bool(full and full["sharpe_ratio"] is not None and full["sharpe_ratio"] >= 1.2),
        "M": bool(full and full["max_drawdown"] >= -0.15),
        "D": bool(wf and wf["sharpe_decay"] is not None and wf["sharpe_decay"] <= 0.30),
        "W": bool(full and full["win_rate"] >= 0.40),
    }
    result["A"] = all(result.values())
    return result


def _candidate_configs() -> list[tuple[str, dict[str, float | int | str]]]:
    variants: dict[str, dict[str, float | int | str]] = {
        "moe_baseline": {},
        "moe_defensive_heavy": {
            "normal_technical_weight": 0.40,
            "normal_fundamental_weight": 0.35,
            "normal_earnings_weight": 0.10,
            "normal_defensive_weight": 0.15,
            "bear_technical_weight": 0.10,
            "bear_fundamental_weight": 0.35,
            "bear_earnings_weight": 0.20,
            "bear_defensive_weight": 0.35,
        },
        "moe_event_defensive_bear": {
            "bear_technical_weight": 0.10,
            "bear_fundamental_weight": 0.30,
            "bear_earnings_weight": 0.25,
            "bear_defensive_weight": 0.35,
        },
        "moe_quality_core": {
            "normal_technical_weight": 0.35,
            "normal_fundamental_weight": 0.45,
            "normal_earnings_weight": 0.10,
            "normal_defensive_weight": 0.10,
            "bear_technical_weight": 0.10,
            "bear_fundamental_weight": 0.45,
            "bear_earnings_weight": 0.15,
            "bear_defensive_weight": 0.30,
        },
        "moe_earnings_core": {
            "normal_technical_weight": 0.40,
            "normal_fundamental_weight": 0.30,
            "normal_earnings_weight": 0.20,
            "normal_defensive_weight": 0.10,
            "bear_technical_weight": 0.10,
            "bear_fundamental_weight": 0.30,
            "bear_earnings_weight": 0.30,
            "bear_defensive_weight": 0.30,
        },
        "moe_more_exposure": {
            "gross_exposure": 0.95,
            "target_vol": 0.14,
            "max_position_pct": 0.035,
            "normal_technical_weight": 0.45,
            "normal_fundamental_weight": 0.35,
            "normal_earnings_weight": 0.10,
            "normal_defensive_weight": 0.10,
        },
    }
    result = []
    for label, updates in variants.items():
        cfg = deepcopy(CONFIG)
        cfg.update(updates)
        result.append((label, cfg))
    return result


def _load_inputs() -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    dict[str, str],
    dict[str, pd.DataFrame],
    pd.DataFrame,
]:
    close, volume, amount, turnover = base._load_aligned_data()
    industry_map = base._load_industries()
    fundamentals = _load_fundamentals()
    earnings_events = v15._load_earnings_events(close.columns)
    print(
        f"data symbols={close.shape[1]} dates={close.shape[0]} "
        f"{close.index.min().date()}->{close.index.max().date()} "
        f"industry={len(industry_map)} fundamentals={len(fundamentals)} "
        f"earnings_symbols={earnings_events['code'].nunique()} events={len(earnings_events)}",
        flush=True,
    )
    return close, volume, amount, turnover, industry_map, fundamentals, earnings_events


def _production_blockers() -> list[str]:
    return [
        "V16 is research-only and is not an approved AI/deep-learning production model",
        "requires approved point-in-time earnings/fundamental/provider evidence",
        "requires exchange-backed futures/ETF provider and position reconciliation evidence",
        "requires approved execution, liquidity, capacity and borrow/short evidence for market-neutral use",
        "requires paper-trading and small-live evidence before production",
    ]


def main() -> None:
    started = time.time()
    mode = os.environ.get("QUANT_V16_MODE", "grid").strip().lower()
    close, volume, amount, turnover, industry_map, fundamentals, earnings_events = _load_inputs()
    if mode == "grid":
        rows: list[dict[str, Any]] = []
        for label, cfg in _candidate_configs():
            full = _hedged_backtest(
                close,
                volume,
                amount,
                turnover,
                fundamentals,
                industry_map,
                earnings_events,
                0,
                len(close),
                cfg,
            )
            row = {"label": label, "full": full, "config": cfg}
            rows.append(row)
            print(f"{label}: {full}", flush=True)
        rows = sorted(
            rows,
            key=lambda row: (
                row["full"]["sharpe_ratio"] if row["full"] else -999.0,
                row["full"]["max_drawdown"] if row["full"] else -999.0,
            ),
            reverse=True,
        )
        report = {
            "ts": datetime.now().isoformat(),
            "version": "V16-multi-expert-regime-router-grid",
            "research_only": True,
            "public_inspiration": [
                "High-Flyer public reporting: AI/deep-learning infrastructure and capacity/regime lessons",
                "Two Sigma public platform notes: many data sources, simulations, independent forecasts and cost/risk-aware portfolio construction",
                "AQR public style investing notes: value, momentum, carry and defensive styles should be diversified rather than used as one standalone factor",
            ],
            "trend_asset_mode": v14.TREND_ASSET_MODE,
            "trend_assets": v14.TREND_ASSETS,
            "production_blockers": _production_blockers(),
            "results": rows,
            "elapsed_s": round(time.time() - started, 1),
        }
        out_path = RESULTS_DIR / "quant_logic_research_v16_moe_router_grid.json"
    elif mode == "finalist":
        label = os.environ.get("QUANT_V16_FINALIST", "moe_baseline").strip()
        cfg_map = {name: cfg for name, cfg in _candidate_configs()}
        if label not in cfg_map:
            raise ValueError(f"unknown finalist {label}; choose from {sorted(cfg_map)}")
        cfg = cfg_map[label]
        full = _hedged_backtest(
            close,
            volume,
            amount,
            turnover,
            fundamentals,
            industry_map,
            earnings_events,
            0,
            len(close),
            cfg,
        )
        print(f"label={label} full={full}", flush=True)
        wf = _walk_forward(
            close,
            volume,
            amount,
            turnover,
            fundamentals,
            industry_map,
            earnings_events,
            cfg,
        )
        report = {
            "ts": datetime.now().isoformat(),
            "version": "V16-multi-expert-regime-router-finalist",
            "label": label,
            "research_only": True,
            "trend_asset_mode": v14.TREND_ASSET_MODE,
            "trend_assets": v14.TREND_ASSETS,
            "production_blockers": _production_blockers(),
            "config": cfg,
            "full": full,
            "wf": wf,
            "gates": _gates(full, wf),
            "elapsed_s": round(time.time() - started, 1),
        }
        out_path = RESULTS_DIR / f"quant_logic_research_v16_{label}_finalist.json"
        print(f"gates={report['gates']}", flush=True)
    else:
        raise ValueError("QUANT_V16_MODE must be grid or finalist")
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {out_path}", flush=True)


if __name__ == "__main__":
    main()
