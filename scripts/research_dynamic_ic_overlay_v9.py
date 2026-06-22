#!/usr/bin/env python3
"""Research V9: dynamic IC weighting plus ETF cash/gold overlay.

This script migrates useful ideas from quant-ashare while keeping strict
point-in-time discipline:
- factor weights use only IC observations whose forward-return labels are known
  before the rebalance date;
- fundamentals use a conservative report-date lag;
- ETF/cash/gold overlay is a research sleeve, not production approval.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
import validate_quant_logic_v5_9 as base
from _paths import PROJECT_DIR, RESULTS_DIR
from research_fundamental_quality_v7 import _fundamental_snapshot, _load_fundamentals, _score

RESULTS_DIR.mkdir(parents=True, exist_ok=True)

CONFIG: dict[str, float | int] = {
    "top_n": 40,
    "rebalance_freq": 10,
    "warmup_days": 756,
    "report_lag_days": 90,
    "min_stocks": 120,
    "max_per_industry": 4,
    "max_position_pct": 0.025,
    "gross_exposure": 0.75,
    "defensive_exposure": 0.35,
    "cash_exposure": 0.05,
    "market_momentum_days": 120,
    "breadth_threshold": 0.52,
    "target_vol": 0.12,
    "ic_window": 180,
    "ic_min_obs": 40,
    "ic_horizon": 5,
    "technical_weight": 0.55,
    "fundamental_weight": 0.45,
    "bear_gold_weight": 0.30,
    "normal_gold_weight": 0.05,
    "dd_reduce_threshold": 0.12,
    "dd_reduce_scale": 0.50,
    "dd_stop_threshold": 0.20,
    "dd_stop_scale": 0.20,
    "stamp_duty": 0.0005,
    "commission": 0.00025,
    "slippage_base": 0.0004,
    "slippage_factor": 0.08,
    "risk_free_rate": 0.025,
    "n_folds": 5,
    "oos_pct": 0.20,
    "purge_days": 10,
}


def _safe_z(series: pd.Series) -> pd.Series:
    values = series.astype(float).replace([np.inf, -np.inf], np.nan)
    lo = values.quantile(0.02)
    hi = values.quantile(0.98)
    values = values.clip(lo, hi)
    std = values.std(skipna=True)
    if not np.isfinite(std) or std < 1e-12:
        return values * np.nan
    return (values - values.mean(skipna=True)) / std


def _technical_factor_matrices(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    amount: pd.DataFrame,
    turnover: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    returns = close.pct_change(fill_method=None)
    market_ret = returns.mean(axis=1, skipna=True)
    excess = returns.sub(market_ret, axis=0)
    ema12 = close.ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = close.ewm(span=26, adjust=False, min_periods=26).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False, min_periods=9).mean()
    ma20 = close.rolling(20, min_periods=20).mean()
    std20 = close.rolling(20, min_periods=20).std()
    vol_ma20 = volume.rolling(20, min_periods=20).mean()
    turnover_ma20 = turnover.rolling(20, min_periods=20).mean()
    turnover_ma5 = turnover.rolling(5, min_periods=5).mean()
    turnover_prior = turnover.rolling(20, min_periods=20).mean().shift(5)
    return {
        "mom_20": close.pct_change(20, fill_method=None),
        "mom_60": close.pct_change(60, fill_method=None),
        "mom_120": close.pct_change(120, fill_method=None),
        "reversal_5": -close.pct_change(5, fill_method=None),
        "low_vol_60": -returns.rolling(60, min_periods=40).std(),
        "idio_vol_60": -excess.rolling(60, min_periods=40).std(),
        "macd_hist": dif - dea,
        "bollinger_mr": -((close - ma20).abs() / (2.0 * std20).replace(0, np.nan)),
        "vol_dev": -((volume / vol_ma20.replace(0, np.nan) - 1.0).abs()),
        "turnover_low": -turnover_ma20,
        "turnover_change": turnover_ma5 / turnover_prior.replace(0, np.nan) - 1.0,
        "amihud_low": -(returns.abs() / amount.replace(0, np.nan)).rolling(20, min_periods=10).mean(),
    }


def _precompute_ic_history(
    factors: dict[str, pd.DataFrame],
    close: pd.DataFrame,
    horizon: int,
) -> pd.DataFrame:
    forward = close.shift(-horizon) / close - 1.0
    forward_rank = forward.rank(axis=1, pct=True)
    ic = {}
    for name, matrix in factors.items():
        ic[name] = matrix.rank(axis=1, pct=True).corrwith(forward_rank, axis=1)
    return pd.DataFrame(ic).replace([np.inf, -np.inf], np.nan)


def _dynamic_ic_weights(
    ic_history: pd.DataFrame,
    date: pd.Timestamp,
    cfg: dict[str, float | int],
) -> dict[str, float]:
    known_until = date - pd.Timedelta(days=int(cfg["ic_horizon"]) + 1)
    window = ic_history.loc[ic_history.index <= known_until].tail(int(cfg["ic_window"]))
    if len(window) < int(cfg["ic_min_obs"]):
        return {column: 1.0 / len(ic_history.columns) for column in ic_history.columns}
    mean = window.mean(skipna=True).clip(lower=0)
    std = window.std(skipna=True).replace(0, np.nan).fillna(0.01)
    raw = mean / std
    total = raw.sum()
    if not np.isfinite(total) or total <= 0:
        return {column: 1.0 / len(ic_history.columns) for column in ic_history.columns}
    return {column: float(raw[column] / total) for column in ic_history.columns}


def _market_exposure(close_window: pd.DataFrame, cfg: dict[str, float | int]) -> tuple[float, bool]:
    lookback = int(cfg["market_momentum_days"])
    if len(close_window) < lookback + 1:
        return 0.0, True
    latest = close_window.iloc[-1].dropna()
    past = close_window.iloc[-lookback - 1].reindex(latest.index)
    momentum = (latest / past.replace(0, np.nan) - 1.0).median(skipna=True)
    breadth = (latest > close_window.tail(lookback).mean(skipna=True).reindex(latest.index)).mean()
    bear = bool(momentum <= 0 and breadth < float(cfg["breadth_threshold"]))
    if momentum > 0 and breadth >= float(cfg["breadth_threshold"]):
        exposure = float(cfg["gross_exposure"])
    elif momentum > 0 or breadth >= float(cfg["breadth_threshold"]):
        exposure = float(cfg["defensive_exposure"])
    else:
        exposure = float(cfg["cash_exposure"])
    realized_vol = close_window.pct_change(fill_method=None).mean(axis=1, skipna=True).tail(20).std()
    ann_vol = float(realized_vol * np.sqrt(252)) if np.isfinite(realized_vol) else np.nan
    if np.isfinite(ann_vol) and ann_vol > 1e-8:
        exposure *= min(1.0, float(cfg["target_vol"]) / ann_vol)
    return float(np.clip(exposure, 0.0, float(cfg["gross_exposure"]))), bear


def _load_overlay_returns(index: pd.DatetimeIndex) -> pd.DataFrame:
    assets = {
        "cash": PROJECT_DIR / "data" / "benchmarks" / "etf_511880.parquet",
        "gold": PROJECT_DIR / "data" / "benchmarks" / "etf_518880.parquet",
    }
    result = {}
    for name, path in assets.items():
        if not path.exists():
            continue
        df = pd.read_parquet(path)
        result[name] = df["close"].astype(float).pct_change(fill_method=None)
    overlay = pd.DataFrame(result).reindex(index).fillna(0.0)
    if "cash" not in overlay.columns:
        overlay["cash"] = 0.0
    if "gold" not in overlay.columns:
        overlay["gold"] = 0.0
    return overlay


def _rebalance_cost(
    new_weights: dict[str, float],
    old_weights: dict[str, float],
    cfg: dict[str, float | int],
) -> tuple[float, float]:
    symbols = set(new_weights) | set(old_weights)
    buy = sum(max(new_weights.get(symbol, 0.0) - old_weights.get(symbol, 0.0), 0.0) for symbol in symbols)
    sell = sum(max(old_weights.get(symbol, 0.0) - new_weights.get(symbol, 0.0), 0.0) for symbol in symbols)
    turnover = buy + sell
    slippage = float(cfg["slippage_base"]) * (1.0 + float(cfg["slippage_factor"]) * np.sqrt(turnover))
    cost = buy * float(cfg["commission"]) + sell * (float(cfg["commission"]) + float(cfg["stamp_duty"]))
    cost += turnover * slippage
    return float(cost), float(turnover)


def _select(score: pd.Series, industry_map: dict[str, str], cfg: dict[str, float | int]) -> list[str]:
    selected: list[str] = []
    counts: Counter[str] = Counter()
    for symbol in score.sort_values(ascending=False).index:
        industry = industry_map.get(str(symbol), f"__UNKNOWN__:{symbol}")
        if counts[industry] >= int(cfg["max_per_industry"]):
            continue
        selected.append(str(symbol))
        counts[industry] += 1
        if len(selected) >= int(cfg["top_n"]):
            break
    return selected


def backtest(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    amount: pd.DataFrame,
    turnover: pd.DataFrame,
    fundamentals: dict[str, pd.DataFrame],
    industry_map: dict[str, str],
    start_idx: int,
    end_idx: int,
    cfg: dict[str, float | int],
    *,
    warmup: int | None = None,
) -> dict[str, Any] | None:
    warmup = int(warmup or cfg["warmup_days"])
    if end_idx - start_idx < warmup + 30:
        return None
    close_slice = close.iloc[start_idx:end_idx]
    factors = _technical_factor_matrices(
        close_slice,
        volume.iloc[start_idx:end_idx],
        amount.iloc[start_idx:end_idx],
        turnover.iloc[start_idx:end_idx],
    )
    ic_history = _precompute_ic_history(factors, close_slice, int(cfg["ic_horizon"]))
    overlay_returns = _load_overlay_returns(close.index)
    positions: dict[str, float] = {}
    overlay_weights = {"cash": 0.0, "gold": 0.0}
    returns: list[float] = []
    turnovers: list[float] = []
    active_counts: list[int] = []
    equity = 1.0
    peak_equity = 1.0
    total_cost = 0.0
    exposure_values: list[float] = []
    last_ic_weights: dict[str, float] = {}
    for t in range(start_idx + warmup, end_idx - 1):
        local_t = t - start_idx
        date = pd.Timestamp(close.index[t])
        cost_today = 0.0
        if (t - start_idx - warmup) % int(cfg["rebalance_freq"]) == 0:
            latest = close.iloc[t].dropna()
            symbols = [symbol for symbol in latest.index if close.iloc[start_idx:t][symbol].count() >= 252]
            tech_weights = _dynamic_ic_weights(ic_history, date, cfg)
            last_ic_weights = tech_weights
            tech_score = pd.Series(0.0, index=symbols)
            tech_total = pd.Series(0.0, index=symbols)
            for name, weight in tech_weights.items():
                values = factors[name].iloc[local_t].reindex(symbols)
                z = _safe_z(values)
                valid = z.notna()
                tech_score.loc[valid] += weight * z.loc[valid]
                tech_total.loc[valid] += weight
            tech_score = (tech_score / tech_total.replace(0, np.nan)).dropna()
            snapshot = _fundamental_snapshot(
                fundamentals, symbols, date, int(cfg["report_lag_days"])
            )
            fund_score = _score(snapshot, latest)
            combined = (
                float(cfg["technical_weight"]) * _safe_z(tech_score)
                + float(cfg["fundamental_weight"]) * _safe_z(fund_score)
            ).dropna()
            if len(combined) < int(cfg["min_stocks"]):
                new_positions = {}
                gross = 0.0
                bear = True
            else:
                close_window = close.iloc[max(start_idx, t - 252) : t + 1][symbols]
                gross, bear = _market_exposure(close_window, cfg)
                current_dd = (equity - peak_equity) / peak_equity
                if current_dd < -float(cfg["dd_stop_threshold"]):
                    gross *= float(cfg["dd_stop_scale"])
                elif current_dd < -float(cfg["dd_reduce_threshold"]):
                    gross *= float(cfg["dd_reduce_scale"])
                selected = _select(combined, industry_map, cfg)
                weight = min(gross / len(selected), float(cfg["max_position_pct"])) if selected else 0.0
                new_positions = {symbol: weight for symbol in selected}
            residual = max(0.0, 1.0 - sum(new_positions.values()))
            gold_weight = residual * (float(cfg["bear_gold_weight"]) if bear else float(cfg["normal_gold_weight"]))
            new_overlay = {"gold": gold_weight, "cash": residual - gold_weight}
            cost_today, turnover_today = _rebalance_cost(new_positions, positions, cfg)
            positions = new_positions
            overlay_weights = new_overlay
            turnovers.append(turnover_today)
            total_cost += cost_today
            active_counts.append(len(positions))
            exposure_values.append(sum(abs(weight) for weight in positions.values()))
        day_return = -cost_today
        current = close.iloc[t]
        nxt = close.iloc[t + 1]
        for symbol, weight in positions.items():
            p0 = current.get(symbol, np.nan)
            p1 = nxt.get(symbol, np.nan)
            if pd.notna(p0) and pd.notna(p1) and p0 > 0 and p1 > 0:
                day_return += weight * (float(p1) / float(p0) - 1.0)
        overlay_day = overlay_returns.iloc[t + 1]
        day_return += overlay_weights.get("cash", 0.0) * float(overlay_day.get("cash", 0.0))
        day_return += overlay_weights.get("gold", 0.0) * float(overlay_day.get("gold", 0.0))
        returns.append(day_return)
        equity *= 1.0 + day_return
        peak_equity = max(peak_equity, equity)
    if len(returns) < 50:
        return None
    arr = np.array(returns, dtype=float)
    ann_return = float(np.mean(arr) * 252)
    ann_vol = float(np.std(arr, ddof=1) * np.sqrt(252))
    sharpe = (ann_return - float(cfg["risk_free_rate"])) / ann_vol if ann_vol > 1e-8 else None
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
        "avg_turnover": round(float(np.mean(turnovers)), 4) if turnovers else 0.0,
        "avg_exposure": round(float(np.mean(exposure_values)), 4) if exposure_values else 0.0,
        "total_cost": round(total_cost, 4),
        "last_ic_weights": {k: round(v, 4) for k, v in sorted(last_ic_weights.items())},
        "n_days": len(arr),
    }


def walk_forward(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    amount: pd.DataFrame,
    turnover: pd.DataFrame,
    fundamentals: dict[str, pd.DataFrame],
    industry_map: dict[str, str],
    cfg: dict[str, float | int],
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
        train = backtest(close, volume, amount, turnover, fundamentals, industry_map, 0, train_end, cfg)
        context_start = max(0, test_start - int(cfg["warmup_days"]))
        oos = backtest(
            close,
            volume,
            amount,
            turnover,
            fundamentals,
            industry_map,
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
            "active": oos["avg_active_stocks"] if oos else None,
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


def gates(full: dict[str, Any] | None, wf: dict[str, Any]) -> dict[str, bool]:
    result = {
        "S": bool(full and full["sharpe_ratio"] is not None and full["sharpe_ratio"] >= 1.2),
        "M": bool(full and full["max_drawdown"] >= -0.15),
        "D": bool(wf["sharpe_decay"] is not None and wf["sharpe_decay"] <= 0.30),
        "W": bool(full and full["win_rate"] >= 0.40),
    }
    result["A"] = all(result.values())
    return result


def main() -> None:
    started = time.time()
    close, volume, amount, turnover = base._load_aligned_data()
    industry_map = base._load_industries()
    fundamentals = _load_fundamentals()
    print(
        f"data symbols={close.shape[1]} dates={close.shape[0]} "
        f"{close.index.min().date()}->{close.index.max().date()} "
        f"industry={len(industry_map)} fundamentals={len(fundamentals)}",
        flush=True,
    )
    full = backtest(close, volume, amount, turnover, fundamentals, industry_map, 0, len(close), CONFIG)
    print(f"full={full}", flush=True)
    wf = walk_forward(close, volume, amount, turnover, fundamentals, industry_map, CONFIG)
    report = {
        "ts": datetime.now().isoformat(),
        "version": "V9-dynamic-ic-overlay",
        "research_only": True,
        "data": {
            "symbols": int(close.shape[1]),
            "dates": int(close.shape[0]),
            "start": str(close.index.min().date()),
            "end": str(close.index.max().date()),
            "industry_mapped": len(industry_map),
            "fundamental_mapped": len(fundamentals),
        },
        "config": CONFIG,
        "full": full,
        "wf": wf,
        "gates": gates(full, wf),
        "elapsed_s": round(time.time() - started, 1),
    }
    out_path = RESULTS_DIR / "quant_logic_research_v9_dynamic_ic_overlay.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {out_path}")
    print(f"gates={report['gates']}")


if __name__ == "__main__":
    main()
