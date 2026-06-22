#!/usr/bin/env python3
"""Research V19: diagnose persistent walk-forward OOS regime weakness.

This script is research-only. It replays a selected V17/V18 candidate over a
walk-forward fold and records trace evidence that the normal backtest omits:
daily returns, exposure, bear-state decisions, alternative-data coverage and
selected industry concentration.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
import research_alt_data_v17 as v17
from _paths import RESULTS_DIR

RESULTS_DIR.mkdir(parents=True, exist_ok=True)

CANDIDATE = os.environ.get("QUANT_V19_CANDIDATE", "v18_revision_stable_freq10")
FOCUS_FOLD = int(os.environ.get("QUANT_V19_FOCUS_FOLD", "1"))


def _candidate_config(label: str) -> dict[str, float | int | str]:
    for name, cfg in v17._candidate_configs():
        if name == label:
            return cfg
    raise ValueError(f"unknown candidate {label}")


def _fold_ranges(index: pd.DatetimeIndex, cfg: dict[str, float | int | str]) -> list[dict[str, Any]]:
    n = len(index)
    oos_total = int(n * float(cfg["oos_pct"]))
    fold_size = oos_total // int(cfg["n_folds"])
    first_test_start = n - oos_total
    purge = int(cfg["purge_days"])
    warmup = int(cfg["warmup_days"])
    rows: list[dict[str, Any]] = []
    for fold in range(int(cfg["n_folds"])):
        test_start = first_test_start + fold * fold_size
        test_end = min(test_start + fold_size, n)
        train_end = test_start - purge
        context_start = max(0, test_start - warmup)
        rows.append(
            {
                "fold": fold,
                "train_end_idx": train_end,
                "test_start_idx": test_start,
                "test_end_idx": test_end,
                "context_start_idx": context_start,
                "train_end": str(index[train_end - 1].date()),
                "test_start": str(index[test_start].date()),
                "test_end": str(index[test_end - 1].date()),
                "context_start": str(index[context_start].date()),
                "days": int(test_end - test_start),
            }
        )
    return rows


def _market_stats(
    close: pd.DataFrame,
    start_idx: int,
    end_idx: int,
    risk_free_rate: float,
) -> dict[str, Any]:
    window = close.iloc[start_idx:end_idx]
    equal_ret = window.pct_change(fill_method=None).mean(axis=1, skipna=True).dropna()
    stats = v17._return_stats(equal_ret.to_numpy(dtype=float), risk_free_rate)
    if equal_ret.empty:
        return {**stats, "worst_days": [], "best_days": []}
    worst = equal_ret.sort_values().head(5)
    best = equal_ret.sort_values(ascending=False).head(5)
    return {
        **stats,
        "worst_days": [
            {"date": str(date.date()), "equal_weight_return": round(float(value), 6)}
            for date, value in worst.items()
        ],
        "best_days": [
            {"date": str(date.date()), "equal_weight_return": round(float(value), 6)}
            for date, value in best.items()
        ],
    }


def _monthly_returns(daily: pd.DataFrame) -> list[dict[str, Any]]:
    if daily.empty:
        return []
    grouped = daily.copy()
    grouped["month"] = grouped["date"].dt.to_period("M").astype(str)
    rows = []
    for month, group in grouped.groupby("month"):
        rows.append(
            {
                "month": month,
                "strategy_return": round(float((1.0 + group["strategy_return"]).prod() - 1.0), 6),
                "universe_return": round(float((1.0 + group["universe_return"]).prod() - 1.0), 6),
                "avg_stock_exposure": round(float(group["stock_exposure"].mean()), 4),
                "avg_total_exposure": round(float(group["total_exposure"].mean()), 4),
            }
        )
    return rows


def _trace_backtest_v17(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    amount: pd.DataFrame,
    turnover: pd.DataFrame,
    fundamentals: dict[str, pd.DataFrame],
    industry_map: dict[str, str],
    earnings_events: pd.DataFrame,
    alt_features: pd.DataFrame,
    crisis_prices: pd.DataFrame,
    crisis_roles: dict[str, str],
    start_idx: int,
    end_idx: int,
    cfg: dict[str, float | int | str],
    *,
    warmup: int,
) -> dict[str, Any]:
    base_cfg: dict[str, float | int] = {
        key: value for key, value in cfg.items() if isinstance(value, int | float)
    }
    close_slice = close.iloc[start_idx:end_idx]
    factors = v17.v9._technical_factor_matrices(
        close_slice,
        volume.iloc[start_idx:end_idx],
        amount.iloc[start_idx:end_idx],
        turnover.iloc[start_idx:end_idx],
    )
    ic_history = v17.v9._precompute_ic_history(factors, close_slice, int(base_cfg["ic_horizon"]))
    trend_prices = v17.v14._load_trend_prices(close.index)
    trend_returns = trend_prices.pct_change(fill_method=None).fillna(0.0)
    crisis_returns = crisis_prices.pct_change(fill_method=None).reindex(close.index).fillna(0.0)
    overlay_returns = pd.concat([trend_returns, crisis_returns], axis=1).fillna(0.0)
    hedge_returns = v17.v14._load_hedge_returns(close.index, str(cfg["hedge_asset"]))
    universe_returns = close.pct_change(fill_method=None).mean(axis=1, skipna=True).fillna(0.0)

    positions: dict[str, float] = {}
    overlay_weights: dict[str, float] = {"cash": 0.0}
    hedge_weight = 0.0
    daily_rows: list[dict[str, Any]] = []
    rebalance_rows: list[dict[str, Any]] = []
    returns: list[float] = []
    return_dates: list[pd.Timestamp] = []
    equity = 1.0
    peak_equity = 1.0
    total_cost = 0.0
    event_cache: dict[pd.Timestamp, pd.Series] = {}
    crisis_asset_set = set(crisis_prices.columns)

    for t in range(start_idx + warmup, end_idx - 1):
        local_t = t - start_idx
        date = pd.Timestamp(close.index[t])
        cost_today = 0.0
        rebalance_info: dict[str, Any] | None = None
        if (t - start_idx - warmup) % int(base_cfg["rebalance_freq"]) == 0:
            latest = close.iloc[t].dropna()
            symbols = [
                str(symbol)
                for symbol in latest.index
                if close.iloc[start_idx:t][symbol].count() >= 252
            ]
            if not symbols:
                new_positions = {}
                gross = 0.0
                bear = True
                alt_coverage = 0
                event_coverage = 0
                selected: list[str] = []
            else:
                close_window = close.iloc[max(start_idx, t - 252) : t + 1][symbols]
                gross, bear = v17.v9._market_exposure(close_window, base_cfg)
                if bear and v17._bear_reentry_signal(close_window, cfg):
                    gross = max(gross, float(cfg.get("bear_reentry_exposure", gross)))
                    if int(cfg.get("bear_reentry_treat_as_normal", 1)):
                        bear = False
                tech_weights = v17.v9._dynamic_ic_weights(ic_history, date, base_cfg)
                tech_score = pd.Series(0.0, index=symbols)
                tech_total = pd.Series(0.0, index=symbols)
                for name, weight_value in tech_weights.items():
                    z = v17._safe_z(factors[name].iloc[local_t].reindex(symbols))
                    valid = z.notna()
                    tech_score.loc[valid] += weight_value * z.loc[valid]
                    tech_total.loc[valid] += weight_value
                tech_score = (tech_score / tech_total.replace(0, np.nan)).dropna()
                snapshot = v17.v9._fundamental_snapshot(
                    fundamentals,
                    symbols,
                    date,
                    int(base_cfg["report_lag_days"]),
                )
                fund_score = v17.v9._score(snapshot, latest)
                if date not in event_cache:
                    event_cache[date] = v17.v15._earnings_event_score(
                        earnings_events, date, cfg
                    )
                event_score = event_cache[date].reindex(symbols)
                event_coverage = int(event_score.notna().sum())
                defensive_score = v17.v16._defensive_expert_score(
                    factors, local_t, symbols, snapshot, cfg
                )
                alt_score, alt_coverage = v17._alt_data_score(alt_features, date, symbols, cfg)
                combined = v17._combine_v17_experts(
                    tech_score,
                    fund_score,
                    event_score,
                    defensive_score,
                    alt_score,
                    symbols,
                    bear,
                    cfg,
                )
                combined = v17.v14._industry_relative_blend(combined, industry_map, cfg)
                current_dd = (equity - peak_equity) / peak_equity
                if current_dd < -float(base_cfg["dd_stop_threshold"]):
                    gross *= float(base_cfg["dd_stop_scale"])
                elif current_dd < -float(base_cfg["dd_reduce_threshold"]):
                    gross *= float(base_cfg["dd_reduce_scale"])
                industry_scores = v17.v14._industry_rotation_scores(close, industry_map, t, cfg)
                combined = v17.v14._apply_industry_rotation(
                    combined, industry_scores, industry_map, bear, cfg
                )
                if len(combined) < int(base_cfg["min_stocks"]):
                    gross *= float(cfg["industry_risk_off_scale"]) if bear else 0.5
                    selected = []
                else:
                    selected = v17.v9._select(combined, industry_map, base_cfg)
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
            trend_overlay = v17.v14._trend_overlay_weights(trend_prices, t, residual, bear, cfg)
            crisis_overlay = v17._crisis_overlay_weights(
                crisis_prices, crisis_roles, t, residual, bear, cfg
            )
            new_overlay = v17._merge_overlay_weights(trend_overlay, crisis_overlay)
            cost_today, turnover_today = v17.v9._rebalance_cost(
                new_positions, positions, base_cfg
            )
            hedge_turnover = abs(new_hedge - hedge_weight)
            cost_today += hedge_turnover * float(cfg["hedge_cost"])
            all_overlay_assets = set(new_overlay) | set(overlay_weights)
            overlay_turnover = sum(
                abs(new_overlay.get(asset, 0.0) - overlay_weights.get(asset, 0.0))
                for asset in all_overlay_assets
                if asset not in crisis_asset_set
            )
            crisis_turnover = sum(
                abs(new_overlay.get(asset, 0.0) - overlay_weights.get(asset, 0.0))
                for asset in all_overlay_assets
                if asset in crisis_asset_set
            )
            cost_today += overlay_turnover * float(cfg["trend_cost"])
            cost_today += crisis_turnover * float(cfg["crisis_cost"])
            positions = new_positions
            overlay_weights = new_overlay
            hedge_weight = new_hedge
            total_cost += cost_today
            industry_counts = Counter(
                industry_map.get(symbol, f"__UNKNOWN__:{symbol}") for symbol in positions
            )
            rebalance_info = {
                "date": str(date.date()),
                "bear": bool(bear),
                "gross": round(float(gross), 6),
                "stock_exposure": round(float(sum(positions.values())), 6),
                "hedge_weight": round(float(hedge_weight), 6),
                "overlay_exposure": round(
                    float(sum(abs(w) for a, w in overlay_weights.items() if a != "cash")), 6
                ),
                "overlay_weights": {
                    str(asset): round(float(weight), 6)
                    for asset, weight in sorted(overlay_weights.items())
                    if abs(float(weight)) > 1e-12
                },
                "active_stocks": len(positions),
                "alt_coverage": int(alt_coverage),
                "event_coverage": int(event_coverage),
                "turnover": round(float(turnover_today), 6),
                "cost": round(float(cost_today), 8),
                "top_industries": industry_counts.most_common(5),
            }
            rebalance_rows.append(rebalance_info)

        day_return = -cost_today
        stock_return = 0.0
        current = close.iloc[t]
        nxt = close.iloc[t + 1]
        for symbol, weight in positions.items():
            p0 = current.get(symbol, np.nan)
            p1 = nxt.get(symbol, np.nan)
            if pd.notna(p0) and pd.notna(p1) and p0 > 0 and p1 > 0:
                contribution = weight * (float(p1) / float(p0) - 1.0)
                stock_return += contribution
                day_return += contribution
        overlay_return = 0.0
        overlay_day = overlay_returns.iloc[t + 1]
        for asset, weight in overlay_weights.items():
            contribution = weight * float(overlay_day.get(asset, 0.0))
            overlay_return += contribution
            day_return += contribution
        hedge_return = hedge_weight * float(hedge_returns.iloc[t + 1])
        day_return += hedge_return
        equity *= 1.0 + day_return
        peak_equity = max(peak_equity, equity)
        return_date = pd.Timestamp(close.index[t + 1])
        returns.append(day_return)
        return_dates.append(return_date)
        daily_rows.append(
            {
                "date": return_date,
                "strategy_return": float(day_return),
                "stock_return": float(stock_return),
                "overlay_return": float(overlay_return),
                "hedge_return": float(hedge_return),
                "cost": float(cost_today),
                "universe_return": float(universe_returns.iloc[t + 1]),
                "stock_exposure": float(sum(positions.values())),
                "hedge_weight": float(hedge_weight),
                "overlay_exposure": float(
                    sum(abs(w) for a, w in overlay_weights.items() if a != "cash")
                ),
                "total_exposure": float(
                    sum(abs(w) for w in positions.values())
                    + abs(hedge_weight)
                    + sum(abs(w) for a, w in overlay_weights.items() if a != "cash")
                ),
                "overlay_weights": {
                    str(asset): round(float(weight), 6)
                    for asset, weight in sorted(overlay_weights.items())
                    if abs(float(weight)) > 1e-12
                },
                "active_stocks": len(positions),
                "rebalance": rebalance_info is not None,
            }
        )

    daily = pd.DataFrame(daily_rows)
    arr = np.array(returns, dtype=float)
    stats = v17._return_stats(arr, float(base_cfg["risk_free_rate"]))
    if daily.empty:
        corr = None
        worst_days: list[dict[str, Any]] = []
        best_days: list[dict[str, Any]] = []
    else:
        corr_value = daily["strategy_return"].corr(daily["universe_return"])
        corr = round(float(corr_value), 4) if pd.notna(corr_value) else None
        worst_days = [
            {
                "date": str(row.date.date()),
                "strategy_return": round(float(row.strategy_return), 6),
                "universe_return": round(float(row.universe_return), 6),
                "stock_return": round(float(row.stock_return), 6),
                "overlay_return": round(float(row.overlay_return), 6),
                "hedge_return": round(float(row.hedge_return), 6),
                "cost": round(float(row.cost), 8),
                "total_exposure": round(float(row.total_exposure), 4),
                "overlay_weights": row.overlay_weights,
            }
            for row in daily.sort_values("strategy_return").head(10).itertuples(index=False)
        ]
        best_days = [
            {
                "date": str(row.date.date()),
                "strategy_return": round(float(row.strategy_return), 6),
                "universe_return": round(float(row.universe_return), 6),
            }
            for row in daily.sort_values("strategy_return", ascending=False)
            .head(5)
            .itertuples(index=False)
        ]

    rebalances = pd.DataFrame(rebalance_rows)
    if rebalances.empty:
        rebalance_summary = {}
    else:
        all_industries: Counter[str] = Counter()
        for row in rebalance_rows:
            all_industries.update(dict(row["top_industries"]))
        rebalance_summary = {
            "n_rebalances": int(len(rebalances)),
            "bear_rebalance_ratio": round(float(rebalances["bear"].mean()), 4),
            "avg_stock_exposure": round(float(rebalances["stock_exposure"].mean()), 4),
            "avg_total_exposure": round(float(daily["total_exposure"].mean()), 4)
            if not daily.empty
            else None,
            "avg_alt_coverage": round(float(rebalances["alt_coverage"].mean()), 1),
            "max_alt_coverage": int(rebalances["alt_coverage"].max()),
            "avg_active_stocks": round(float(rebalances["active_stocks"].mean()), 1),
            "top_selected_industries": all_industries.most_common(10),
        }

    return {
        "stats": stats,
        "correlation_to_universe": corr,
        "total_cost": round(total_cost, 6),
        "rebalance_summary": rebalance_summary,
        "worst_days": worst_days,
        "best_days": best_days,
        "monthly_returns": _monthly_returns(daily),
    }


def main() -> None:
    started = time.time()
    cfg = _candidate_config(CANDIDATE)
    (
        close,
        volume,
        amount,
        turnover,
        industry_map,
        fundamentals,
        earnings_events,
        alt_features,
        crisis_prices,
        crisis_roles,
    ) = v17._load_inputs()
    ranges = _fold_ranges(close.index, cfg)
    base_cfg = {key: value for key, value in cfg.items() if isinstance(value, int | float)}
    market = [
        {
            **row,
            "market_equal_weight": _market_stats(
                close,
                int(row["test_start_idx"]),
                int(row["test_end_idx"]),
                float(base_cfg["risk_free_rate"]),
            ),
        }
        for row in ranges
    ]
    focus = ranges[FOCUS_FOLD]
    trace = _trace_backtest_v17(
        close,
        volume,
        amount,
        turnover,
        fundamentals,
        industry_map,
        earnings_events,
        alt_features,
        crisis_prices,
        crisis_roles,
        int(focus["context_start_idx"]),
        int(focus["test_end_idx"]),
        cfg,
        warmup=int(focus["test_start_idx"]) - int(focus["context_start_idx"]),
    )
    report = {
        "ts": datetime.now().isoformat(),
        "version": "V19-oos-regime-diagnostics",
        "research_only": True,
        "candidate": CANDIDATE,
        "focus_fold": FOCUS_FOLD,
        "stock_alt_feature_file": v17.STOCK_ALT_FEATURE_FILE,
        "fold_ranges": ranges,
        "fold_market_summary": market,
        "focus_trace": trace,
        "production_blockers": [
            "diagnostic evidence is not a production strategy",
            "candidate still fails Sharpe>=1.2 production gate",
            "negative OOS fold must be fixed before any production promotion",
        ],
        "elapsed_s": round(time.time() - started, 1),
    }
    safe_candidate = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in CANDIDATE)
    out_path = (
        RESULTS_DIR
        / f"quant_logic_research_v19_oos_regime_diagnostics_{safe_candidate}_f{FOCUS_FOLD}.json"
    )
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {out_path}")
    print(
        f"focus F{FOCUS_FOLD} stats={trace['stats']} "
        f"rebalance={trace['rebalance_summary']}"
    )


if __name__ == "__main__":
    main()
