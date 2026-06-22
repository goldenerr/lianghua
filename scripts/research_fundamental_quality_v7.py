#!/usr/bin/env python3
"""Research V7: point-in-time-lagged fundamental quality/value factors.

This script uses THS financial abstracts with a conservative report-date lag.
It is research only: no production approval, no configuration changes.
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
from _paths import FUNDAMENTALS_DIR, RESULTS_DIR

RESULTS_DIR.mkdir(parents=True, exist_ok=True)

CONFIG: dict[str, float | int] = {
    "top_n": 60,
    "rebalance_freq": 60,
    "warmup_days": 504,
    "test_warmup_days": 504,
    "report_lag_days": 90,
    "min_stocks": 100,
    "max_per_industry": 5,
    "max_position_pct": 0.03,
    "gross_exposure": 1.0,
    "defensive_exposure": 0.35,
    "cash_exposure": 0.05,
    "market_momentum_days": 120,
    "breadth_threshold": 0.50,
    "target_vol": 0.16,
    "dd_reduce_threshold": 0.15,
    "dd_reduce_scale": 0.50,
    "dd_stop_threshold": 0.25,
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

FACTOR_WEIGHTS = {
    "roe": 0.18,
    "gross_margin": 0.12,
    "debt_asset_ratio": -0.12,
    "revenue_yoy": 0.12,
    "net_profit_yoy": 0.10,
    "book_to_price": 0.18,
    "earnings_to_price": 0.10,
    "cashflow_quality": 0.10,
}


def _safe_z(series: pd.Series) -> pd.Series:
    values = series.astype(float).replace([np.inf, -np.inf], np.nan)
    lo = values.quantile(0.02)
    hi = values.quantile(0.98)
    values = values.clip(lo, hi)
    mean = values.mean(skipna=True)
    std = values.std(skipna=True)
    if not np.isfinite(std) or std < 1e-12:
        return values * np.nan
    return (values - mean) / std


def _load_fundamentals() -> dict[str, pd.DataFrame]:
    fundamentals: dict[str, pd.DataFrame] = {}
    for file_path in sorted(FUNDAMENTALS_DIR.glob("*.parquet")):
        if file_path.stem == "fetch_summary":
            continue
        df = pd.read_parquet(file_path).sort_index()
        if df.empty:
            continue
        fundamentals[file_path.stem] = df
    return fundamentals


def _fundamental_snapshot(
    fundamentals: dict[str, pd.DataFrame],
    symbols: list[str],
    asof_date: pd.Timestamp,
    lag_days: int,
) -> pd.DataFrame:
    usable_date = asof_date - pd.Timedelta(days=lag_days)
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        df = fundamentals.get(symbol)
        if df is None or df.empty:
            continue
        pos = df.index.searchsorted(usable_date, side="right") - 1
        if pos < 0:
            continue
        row = df.iloc[pos]
        rows.append(
            {
                "symbol": symbol,
                "roe": row.get("roe", np.nan),
                "gross_margin": row.get("gross_margin", np.nan),
                "debt_asset_ratio": row.get("debt_asset_ratio", np.nan),
                "revenue_yoy": row.get("revenue_yoy", np.nan),
                "net_profit_yoy": row.get("net_profit_yoy", np.nan),
                "eps": row.get("eps", np.nan),
                "book_value_per_share": row.get("book_value_per_share", np.nan),
                "operating_cash_flow_per_share": row.get("operating_cash_flow_per_share", np.nan),
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("symbol")


def _score(
    snapshot: pd.DataFrame,
    latest_close: pd.Series,
) -> pd.Series:
    if snapshot.empty:
        return pd.Series(dtype=float)
    data = snapshot.copy()
    price = latest_close.reindex(data.index).astype(float).replace(0, np.nan)
    data["book_to_price"] = data["book_value_per_share"].astype(float) / price
    data["earnings_to_price"] = data["eps"].astype(float) / price
    data["cashflow_quality"] = data["operating_cash_flow_per_share"].astype(float) / data[
        "book_value_per_share"
    ].astype(float).replace(0, np.nan)
    score = pd.Series(0.0, index=data.index)
    total = pd.Series(0.0, index=data.index)
    for factor, weight in FACTOR_WEIGHTS.items():
        z = _safe_z(data[factor])
        valid = z.notna()
        score.loc[valid] += abs(weight) * (z.loc[valid] if weight > 0 else -z.loc[valid])
        total.loc[valid] += abs(weight)
    return (score / total.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).dropna()


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


def _market_exposure(close_window: pd.DataFrame, cfg: dict[str, float | int]) -> float:
    lookback = int(cfg["market_momentum_days"])
    if len(close_window) < lookback + 1:
        return 0.0
    latest = close_window.iloc[-1].dropna()
    past = close_window.iloc[-lookback - 1].reindex(latest.index)
    if latest.empty:
        return 0.0
    momentum = (latest / past.replace(0, np.nan) - 1.0).median(skipna=True)
    ma = close_window.tail(lookback).mean(skipna=True).reindex(latest.index)
    breadth = (latest > ma).mean()
    if momentum > 0 and breadth >= float(cfg["breadth_threshold"]):
        exposure = float(cfg["gross_exposure"])
    elif momentum > 0 or breadth >= float(cfg["breadth_threshold"]):
        exposure = float(cfg["defensive_exposure"])
    else:
        exposure = float(cfg["cash_exposure"])
    market_returns = close_window.pct_change(fill_method=None).mean(axis=1, skipna=True).tail(20)
    realized_vol = (
        float(market_returns.std(ddof=1) * np.sqrt(252)) if len(market_returns) >= 10 else np.nan
    )
    if np.isfinite(realized_vol) and realized_vol > 1e-8:
        exposure *= min(1.0, float(cfg["target_vol"]) / realized_vol)
    return float(np.clip(exposure, 0.0, float(cfg["gross_exposure"])))


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
    positions: dict[str, float] = {}
    returns: list[float] = []
    turnovers: list[float] = []
    active_counts: list[int] = []
    candidate_counts: list[int] = []
    total_cost = 0.0
    equity = 1.0
    peak_equity = 1.0
    exposure_values: list[float] = []
    for t in range(start_idx + warmup, end_idx - 1):
        cost_today = 0.0
        if (t - start_idx - warmup) % int(cfg["rebalance_freq"]) == 0:
            latest = close.iloc[t].dropna()
            symbols = [
                symbol for symbol in latest.index if close.iloc[start_idx:t][symbol].count() >= 252
            ]
            snapshot = _fundamental_snapshot(
                fundamentals,
                symbols,
                pd.Timestamp(close.index[t]),
                int(cfg["report_lag_days"]),
            )
            candidate_counts.append(len(snapshot))
            if len(snapshot) < int(cfg["min_stocks"]):
                new_positions: dict[str, float] = {}
            else:
                score = _score(snapshot, latest)
                selected = _select(score, industry_map, cfg)
                close_window = close.iloc[max(start_idx, t - 252) : t + 1][symbols]
                gross = _market_exposure(close_window, cfg)
                current_dd = (equity - peak_equity) / peak_equity
                if current_dd < -float(cfg["dd_stop_threshold"]):
                    gross *= float(cfg["dd_stop_scale"])
                elif current_dd < -float(cfg["dd_reduce_threshold"]):
                    gross *= float(cfg["dd_reduce_scale"])
                if selected:
                    weight = min(gross / len(selected), float(cfg["max_position_pct"]))
                    new_positions = {symbol: weight for symbol in selected}
                else:
                    new_positions = {}
                active_counts.append(len(new_positions))
                exposure_values.append(sum(abs(weight) for weight in new_positions.values()))
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
            if pd.notna(p0) and pd.notna(p1) and p0 > 0 and p1 > 0:
                day_return += weight * (float(p1) / float(p0) - 1.0)
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
        "avg_candidate_stocks": (
            round(float(np.mean(candidate_counts)), 1) if candidate_counts else 0.0
        ),
        "avg_turnover": round(float(np.mean(turnovers)), 4) if turnovers else 0.0,
        "avg_exposure": round(float(np.mean(exposure_values)), 4) if exposure_values else 0.0,
        "total_cost": round(total_cost, 4),
        "n_days": len(arr),
    }


def walk_forward(
    close: pd.DataFrame,
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
        train = backtest(close, fundamentals, industry_map, 0, train_end, cfg)
        context_start = max(0, test_start - int(cfg["warmup_days"]))
        oos = backtest(
            close,
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
    close, _volume, _amount, _turnover = base._load_aligned_data()
    industry_map = base._load_industries()
    fundamentals = _load_fundamentals()
    print(
        f"data symbols={close.shape[1]} dates={close.shape[0]} "
        f"{close.index.min().date()}->{close.index.max().date()} "
        f"industry={len(industry_map)} fundamentals={len(fundamentals)}",
        flush=True,
    )
    full = backtest(close, fundamentals, industry_map, 0, len(close), CONFIG)
    print(f"full={full}", flush=True)
    wf = walk_forward(close, fundamentals, industry_map, CONFIG)
    report = {
        "ts": datetime.now().isoformat(),
        "version": "V7-fundamental-quality-value",
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
        "factor_weights": FACTOR_WEIGHTS,
        "full": full,
        "wf": wf,
        "gates": gates(full, wf),
        "elapsed_s": round(time.time() - started, 1),
    }
    out_path = RESULTS_DIR / "quant_logic_research_v7_fundamental_quality.json"
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"Wrote {out_path}")
    print(f"gates={report['gates']}")


if __name__ == "__main__":
    main()
