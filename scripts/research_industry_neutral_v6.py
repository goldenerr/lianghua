#!/usr/bin/env python3
"""Research V6: industry-aware long-only strategy with regime risk control.

This is a research script, not production approval. It combines public,
institutional-style ideas that are appropriate for A-share long-only research:
cross-sectional multi-factor ranking, industry diversification, market regime
filtering, volatility targeting and transaction-cost deduction.
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
from _paths import RESULTS_DIR

RESULTS_DIR.mkdir(parents=True, exist_ok=True)

CONFIG: dict[str, float | int] = {
    "top_n": 50,
    "rebalance_freq": 20,
    "warmup_days": 252,
    "test_warmup_days": 252,
    "min_stocks": 100,
    "max_per_industry": 4,
    "max_position_pct": 0.04,
    "stamp_duty": 0.0005,
    "commission": 0.00025,
    "slippage_base": 0.0004,
    "slippage_factor": 0.08,
    "risk_free_rate": 0.025,
    "target_vol": 0.10,
    "max_gross_exposure": 1.0,
    "defensive_exposure": 0.35,
    "cash_exposure": 0.0,
    "market_momentum_days": 120,
    "breadth_threshold": 0.48,
    "n_folds": 5,
    "oos_pct": 0.20,
    "purge_days": 10,
}

WEIGHTS = {
    "mom_12_1": 0.30,
    "reversal_20": 0.15,
    "low_vol_60": 0.25,
    "liquidity_20": 0.10,
    "volume_stability": 0.10,
    "trend_quality": 0.10,
}


def _safe_z(values: pd.Series) -> pd.Series:
    values = values.replace([np.inf, -np.inf], np.nan)
    mean = values.mean(skipna=True)
    std = values.std(skipna=True)
    if not np.isfinite(std) or std < 1e-12:
        return values * np.nan
    return (values - mean) / std


def _score_universe(
    close_window: pd.DataFrame,
    volume_window: pd.DataFrame,
    amount_window: pd.DataFrame,
) -> pd.Series:
    close_window = close_window.where(close_window > 0).replace([np.inf, -np.inf], np.nan)
    volume_window = volume_window.where(volume_window >= 0).replace([np.inf, -np.inf], np.nan)
    amount_window = amount_window.where(amount_window >= 0).replace([np.inf, -np.inf], np.nan)
    if len(close_window) < 252:
        return pd.Series(dtype=float)
    latest = close_window.iloc[-1]
    mom_12_1 = latest / close_window.shift(21).iloc[-252] - 1.0
    reversal_20 = -(latest / close_window.iloc[-21] - 1.0)
    returns = close_window.pct_change(fill_method=None)
    low_vol_60 = -returns.tail(60).std(skipna=True) * np.sqrt(252)
    liquidity_20 = np.log1p(amount_window.tail(20).mean(skipna=True))
    volume_mean = volume_window.tail(60).mean(skipna=True)
    volume_std = volume_window.tail(60).std(skipna=True)
    volume_stability = -(volume_std / volume_mean.replace(0, np.nan))
    ma60 = close_window.tail(60).mean(skipna=True)
    ma120 = close_window.tail(120).mean(skipna=True)
    trend_quality = latest / ma120.replace(0, np.nan) - 1.0
    trend_quality = trend_quality.where(ma60 > ma120)
    factors = {
        "mom_12_1": mom_12_1,
        "reversal_20": reversal_20,
        "low_vol_60": low_vol_60,
        "liquidity_20": liquidity_20,
        "volume_stability": volume_stability,
        "trend_quality": trend_quality,
    }
    score = pd.Series(0.0, index=latest.index)
    total_weight = pd.Series(0.0, index=latest.index)
    for name, factor in factors.items():
        z = _safe_z(factor)
        valid = z.notna()
        score.loc[valid] += float(WEIGHTS[name]) * z.loc[valid]
        total_weight.loc[valid] += float(WEIGHTS[name])
    return (score / total_weight.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).dropna()


def _market_exposure(close_window: pd.DataFrame, cfg: dict[str, float | int]) -> float:
    lookback = int(cfg["market_momentum_days"])
    latest = close_window.iloc[-1].dropna()
    if len(close_window) < lookback + 1 or latest.empty:
        return 0.0
    past = close_window.iloc[-lookback - 1].reindex(latest.index)
    momentum = (latest / past.replace(0, np.nan) - 1.0).median(skipna=True)
    ma = close_window.tail(lookback).mean(skipna=True).reindex(latest.index)
    breadth = (latest > ma).mean()
    if momentum > 0 and breadth >= float(cfg["breadth_threshold"]):
        regime_exposure = float(cfg["max_gross_exposure"])
    elif momentum > 0 or breadth >= float(cfg["breadth_threshold"]):
        regime_exposure = float(cfg["defensive_exposure"])
    else:
        regime_exposure = float(cfg["cash_exposure"])
    returns = close_window.pct_change(fill_method=None).mean(axis=1, skipna=True).tail(20)
    realized_vol = float(returns.std(ddof=1) * np.sqrt(252)) if len(returns) >= 10 else np.nan
    if np.isfinite(realized_vol) and realized_vol > 1e-8:
        regime_exposure *= min(1.0, float(cfg["target_vol"]) / realized_vol)
    return float(np.clip(regime_exposure, 0.0, float(cfg["max_gross_exposure"])))


def _select(
    score: pd.Series,
    industry_map: dict[str, str],
    cfg: dict[str, float | int],
) -> list[str]:
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


def _rebalance_cost(
    new_weights: dict[str, float],
    old_weights: dict[str, float],
    cfg: dict[str, float | int],
) -> tuple[float, float]:
    symbols = set(new_weights) | set(old_weights)
    buy = sum(max(new_weights.get(s, 0.0) - old_weights.get(s, 0.0), 0.0) for s in symbols)
    sell = sum(max(old_weights.get(s, 0.0) - new_weights.get(s, 0.0), 0.0) for s in symbols)
    turnover = buy + sell
    slippage = float(cfg["slippage_base"]) * (
        1.0 + float(cfg["slippage_factor"]) * np.sqrt(turnover)
    )
    cost = buy * float(cfg["commission"]) + sell * (
        float(cfg["commission"]) + float(cfg["stamp_duty"])
    )
    cost += turnover * slippage
    return float(cost), float(turnover)


def backtest(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    amount: pd.DataFrame,
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
    positions: dict[str, float] = {}
    returns: list[float] = []
    active_counts: list[int] = []
    exposures: list[float] = []
    turnovers: list[float] = []
    total_cost = 0.0
    for t in range(start_idx + warmup, end_idx - 1):
        cost_today = 0.0
        if (t - start_idx - warmup) % int(cfg["rebalance_freq"]) == 0:
            window = slice(max(start_idx, t - 252), t + 1)
            close_window = close.iloc[window]
            latest = close.iloc[t].dropna()
            symbols = [s for s in latest.index if close_window[s].count() >= 180]
            if len(symbols) < int(cfg["min_stocks"]):
                new_positions: dict[str, float] = {}
            else:
                score = _score_universe(
                    close_window[symbols],
                    volume.iloc[window][symbols],
                    amount.iloc[window][symbols],
                )
                exposure = _market_exposure(close_window[symbols], cfg)
                selected = _select(score, industry_map, cfg) if exposure > 0 else []
                if selected:
                    raw_weight = min(
                        exposure / len(selected),
                        float(cfg["max_position_pct"]),
                    )
                    new_positions = {symbol: raw_weight for symbol in selected}
                else:
                    new_positions = {}
                active_counts.append(len(selected))
                exposures.append(sum(new_positions.values()))
            cost_today, turnover = _rebalance_cost(new_positions, positions, cfg)
            total_cost += cost_today
            turnovers.append(turnover)
            positions = new_positions
        day_return = -cost_today
        current = close.iloc[t]
        nxt = close.iloc[t + 1]
        for symbol, weight in positions.items():
            p0 = current.get(symbol, np.nan)
            p1 = nxt.get(symbol, np.nan)
            if pd.isna(p0) or pd.isna(p1) or p0 <= 0 or p1 <= 0:
                continue
            day_return += weight * (float(p1) / float(p0) - 1.0)
        returns.append(day_return)
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
        "avg_exposure": round(float(np.mean(exposures)), 4) if exposures else 0.0,
        "avg_turnover": round(float(np.mean(turnovers)), 4) if turnovers else 0.0,
        "total_cost": round(total_cost, 4),
        "n_days": len(arr),
    }


def walk_forward(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    amount: pd.DataFrame,
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
        train = backtest(close, volume, amount, industry_map, 0, train_end, cfg)
        oos_context_start = max(0, test_start - int(cfg["warmup_days"]))
        oos = backtest(
            close,
            volume,
            amount,
            industry_map,
            oos_context_start,
            test_end,
            cfg,
            warmup=test_start - oos_context_start,
        )
        row = {
            "fold": fold,
            "is": train["sharpe_ratio"] if train else None,
            "oos": oos["sharpe_ratio"] if oos else None,
            "mdd": oos["max_drawdown"] if oos else None,
            "exposure": oos["avg_exposure"] if oos else None,
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
    close, volume, amount, _turnover = base._load_aligned_data()
    industry_map = base._load_industries()
    print(
        f"data symbols={close.shape[1]} dates={close.shape[0]} "
        f"{close.index.min().date()}->{close.index.max().date()} "
        f"industry={len(industry_map)}",
        flush=True,
    )
    full = backtest(close, volume, amount, industry_map, 0, len(close), CONFIG)
    print(f"full={full}", flush=True)
    wf = walk_forward(close, volume, amount, industry_map, CONFIG)
    report = {
        "ts": datetime.now().isoformat(),
        "version": "V6-industry-neutral-regime-risk",
        "research_only": True,
        "data": {
            "symbols": int(close.shape[1]),
            "dates": int(close.shape[0]),
            "start": str(close.index.min().date()),
            "end": str(close.index.max().date()),
            "industry_mapped": len(industry_map),
            "industry_coverage": round(
                len(set(close.columns) & set(industry_map)) / close.shape[1], 4
            ),
        },
        "config": CONFIG,
        "weights": WEIGHTS,
        "full": full,
        "wf": wf,
        "gates": gates(full, wf),
        "elapsed_s": round(time.time() - started, 1),
    }
    out_path = RESULTS_DIR / "quant_logic_research_v6_industry_neutral.json"
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"Wrote {out_path}")
    print(f"gates={report['gates']}")


if __name__ == "__main__":
    main()
