#!/usr/bin/env python3
"""Research V15: point-in-time earnings event alpha.

This candidate starts from the strongest V14 configuration and blends a
point-in-time earnings announcement score into the stock-selection layer.
Only events announced before the rebalance date minus a configured lag are
visible, so report dates cannot leak future information into the research.
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
import research_industry_relative_v14 as v14
import validate_quant_logic_v5_9 as base
from _paths import EARNINGS_EVENTS_DIR, RESULTS_DIR
from research_fundamental_quality_v7 import _load_fundamentals

RESULTS_DIR.mkdir(parents=True, exist_ok=True)

CONFIG: dict[str, float | int | str] = deepcopy(v14.CONFIG)
CONFIG.update(
    {
        "gross_exposure": 0.85,
        "max_position_pct": 0.03,
        "industry_relative_weight": 0.20,
        "event_weight": 0.15,
        "event_lag_days": 1,
        "event_lookback_days": 180,
        "event_half_life_days": 90,
        "event_min_symbols": 80,
        "event_direction_weight": 0.35,
        "event_profit_growth_weight": 0.35,
        "event_revenue_growth_weight": 0.15,
        "event_roe_weight": 0.10,
        "event_frequency_weight": 0.05,
    }
)

FORECAST_TYPE_SCORE = {
    "预增": 1.0,
    "略增": 0.6,
    "扭亏": 0.9,
    "续盈": 0.4,
    "减亏": 0.35,
    "预减": -0.8,
    "略减": -0.5,
    "首亏": -1.0,
    "续亏": -0.9,
    "增亏": -0.9,
    "不确定": -0.15,
    "flash": 0.0,
}


def _bounded_metric(series: pd.Series, lower: float, upper: float) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
    return values.clip(lower=lower, upper=upper)


def _event_z(series: pd.Series) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
    result = pd.Series(np.nan, index=series.index, dtype=float)
    finite = values.dropna()
    if len(finite) < 2:
        return result
    lo = finite.quantile(0.02)
    hi = finite.quantile(0.98)
    clipped = finite.clip(lo, hi)
    std = float(np.std(clipped.to_numpy(dtype=float), ddof=1))
    if not np.isfinite(std) or std < 1e-12:
        return result
    result.loc[clipped.index] = (clipped - float(clipped.mean())) / std
    return result


def _load_earnings_events(symbols: pd.Index) -> pd.DataFrame:
    path = EARNINGS_EVENTS_DIR / "earnings_events.parquet"
    if not path.exists():
        raise RuntimeError(f"missing earnings event data: {path}")
    events = pd.read_parquet(path)
    required = {"code", "report_date", "announcement_date", "event_type", "forecast_type"}
    missing = sorted(required - set(events.columns))
    if missing:
        raise RuntimeError(f"earnings event data missing columns: {missing}")
    local_symbols = {str(symbol).zfill(6) for symbol in symbols}
    events = events.copy()
    events["code"] = events["code"].astype(str).str.zfill(6)
    events = events[events["code"].isin(local_symbols)].copy()
    events["announcement_date"] = pd.to_datetime(events["announcement_date"], errors="coerce")
    events["report_date"] = pd.to_datetime(events["report_date"], errors="coerce")
    events = events.dropna(subset=["announcement_date", "report_date"])
    events = events.sort_values(["announcement_date", "report_date", "event_type", "code"])
    return events.reset_index(drop=True)


def _latest_event_snapshot(
    events: pd.DataFrame,
    date: pd.Timestamp,
    cfg: dict[str, float | int | str],
) -> pd.DataFrame:
    known_until = pd.Timestamp(date).normalize() - pd.Timedelta(days=int(cfg["event_lag_days"]))
    lookback_start = known_until - pd.Timedelta(days=int(cfg["event_lookback_days"]))
    recent = events[
        (events["announcement_date"] <= known_until)
        & (events["announcement_date"] >= lookback_start)
    ].copy()
    if recent.empty:
        return recent
    recent["event_count"] = recent.groupby("code")["code"].transform("size")
    recent = recent.sort_values(["code", "announcement_date", "report_date", "event_type"])
    latest = recent.groupby("code", as_index=False).tail(1).copy()
    age_days = (known_until - latest["announcement_date"]).dt.days.clip(lower=0)
    latest["recency_decay"] = np.exp(-age_days / float(cfg["event_half_life_days"]))
    return latest.set_index("code", drop=False)


def _earnings_event_score(
    events: pd.DataFrame,
    date: pd.Timestamp,
    cfg: dict[str, float | int | str],
) -> pd.Series:
    latest = _latest_event_snapshot(events, date, cfg)
    if latest.empty:
        return pd.Series(dtype=float)
    direction = latest["forecast_type"].map(FORECAST_TYPE_SCORE).fillna(0.0).astype(float)
    forecast_growth = latest[["net_profit_yoy_low", "net_profit_yoy_high"]].mean(
        axis=1,
        skipna=True,
    )
    profit_growth = _bounded_metric(latest["net_profit_yoy"].combine_first(forecast_growth), -2.0, 5.0)
    revenue_growth = _bounded_metric(latest["revenue_yoy"], -1.0, 3.0)
    roe = _bounded_metric(latest["roe"], -0.5, 0.8)
    event_count = np.log1p(latest["event_count"].astype(float))
    raw = (
        float(cfg["event_direction_weight"]) * _event_z(direction).fillna(0.0)
        + float(cfg["event_profit_growth_weight"]) * _event_z(profit_growth).fillna(0.0)
        + float(cfg["event_revenue_growth_weight"]) * _event_z(revenue_growth).fillna(0.0)
        + float(cfg["event_roe_weight"]) * _event_z(roe).fillna(0.0)
        + float(cfg["event_frequency_weight"]) * _event_z(event_count).fillna(0.0)
    )
    raw = raw * (0.50 + 0.50 * latest["recency_decay"].astype(float))
    score = _event_z(raw.replace([np.inf, -np.inf], np.nan))
    return score.dropna()


def _blend_event_score(
    combined: pd.Series,
    event_score: pd.Series,
    cfg: dict[str, float | int | str],
) -> pd.Series:
    weight = float(cfg["event_weight"])
    if combined.empty or event_score.empty or weight <= 0:
        return combined
    aligned_event = event_score.reindex(combined.index)
    if int(aligned_event.notna().sum()) < int(cfg["event_min_symbols"]):
        return combined
    base_score = _event_z(combined)
    event_z = _event_z(aligned_event).reindex(combined.index).fillna(0.0)
    blended = (1.0 - weight) * base_score + weight * event_z
    return blended.replace([np.inf, -np.inf], np.nan).dropna()


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
            tech_weights = v9._dynamic_ic_weights(ic_history, date, base_cfg)
            tech_score = pd.Series(0.0, index=symbols)
            tech_total = pd.Series(0.0, index=symbols)
            for name, weight in tech_weights.items():
                z = v9._safe_z(factors[name].iloc[local_t].reindex(symbols))
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
            combined = (
                float(base_cfg["technical_weight"]) * v9._safe_z(tech_score)
                + float(base_cfg["fundamental_weight"]) * v9._safe_z(fund_score)
            ).dropna()
            combined = v14._industry_relative_blend(combined, industry_map, cfg)
            if date not in event_cache:
                event_cache[date] = _earnings_event_score(earnings_events, date, cfg)
            event_score = event_cache[date].reindex(symbols)
            event_counts.append(int(event_score.notna().sum()))
            combined = _blend_event_score(combined, event_score, cfg)
            if len(combined) < int(base_cfg["min_stocks"]):
                new_positions: dict[str, float] = {}
                gross = 0.0
                bear = True
            else:
                close_window = close.iloc[max(start_idx, t - 252) : t + 1][symbols]
                gross, bear = v9._market_exposure(close_window, base_cfg)
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
    candidates: list[tuple[str, dict[str, float | int | str]]] = []
    for weight in (0.0, 0.15, 0.25, 0.35, 0.45):
        cfg = deepcopy(CONFIG)
        cfg["event_weight"] = weight
        candidates.append((f"event_w_{weight:.2f}".replace(".", ""), cfg))
    for label, updates in {
        "event_short_lookback": {
            "event_weight": 0.25,
            "event_lookback_days": 90,
            "event_half_life_days": 45,
        },
        "event_long_lookback": {
            "event_weight": 0.25,
            "event_lookback_days": 270,
            "event_half_life_days": 120,
        },
        "event_direction_heavy": {
            "event_weight": 0.30,
            "event_direction_weight": 0.50,
            "event_profit_growth_weight": 0.25,
            "event_revenue_growth_weight": 0.10,
        },
        "event_growth_heavy": {
            "event_weight": 0.30,
            "event_direction_weight": 0.20,
            "event_profit_growth_weight": 0.50,
            "event_revenue_growth_weight": 0.15,
        },
    }.items():
        cfg = deepcopy(CONFIG)
        cfg.update(updates)
        candidates.append((label, cfg))
    return candidates


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
    earnings_events = _load_earnings_events(close.columns)
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
        "requires approved point-in-time earnings event provider evidence",
        "requires exchange-backed futures/ETF provider and position reconciliation evidence",
        "requires futures margin and rollover approval evidence",
        "requires approved ETF execution, liquidity and capacity evidence",
        "requires paper-trading and small-live evidence before production",
    ]


def main() -> None:
    started = time.time()
    mode = os.environ.get("QUANT_V15_MODE", "grid").strip().lower()
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
            "version": "V15-earnings-events-grid",
            "research_only": True,
            "trend_asset_mode": v14.TREND_ASSET_MODE,
            "trend_assets": v14.TREND_ASSETS,
            "production_blockers": _production_blockers(),
            "results": rows,
            "elapsed_s": round(time.time() - started, 1),
        }
        out_path = RESULTS_DIR / "quant_logic_research_v15_earnings_events_grid.json"
    elif mode == "finalist":
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
            CONFIG,
        )
        print(f"full={full}", flush=True)
        wf = _walk_forward(
            close,
            volume,
            amount,
            turnover,
            fundamentals,
            industry_map,
            earnings_events,
            CONFIG,
        )
        report = {
            "ts": datetime.now().isoformat(),
            "version": "V15-earnings-events-finalist",
            "research_only": True,
            "trend_asset_mode": v14.TREND_ASSET_MODE,
            "trend_assets": v14.TREND_ASSETS,
            "production_blockers": _production_blockers(),
            "config": CONFIG,
            "full": full,
            "wf": wf,
            "gates": _gates(full, wf),
            "elapsed_s": round(time.time() - started, 1),
        }
        out_path = RESULTS_DIR / "quant_logic_research_v15_earnings_events_finalist.json"
        print(f"gates={report['gates']}", flush=True)
    else:
        raise ValueError("QUANT_V15_MODE must be grid or finalist")
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {out_path}", flush=True)


if __name__ == "__main__":
    main()
