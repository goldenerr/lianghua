#!/usr/bin/env python3
"""Validate V5.9 quant logic on aligned 25-year data with large-cap exclusion.

This script is research validation only. It fixes two issues common in quick
research scripts: stocks are aligned by trading date, not array index, and
rebalance costs are deducted from returns.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from _paths import DATA_DIR, PROJECT_DIR, RESULTS_DIR

RESULTS_DIR.mkdir(parents=True, exist_ok=True)

CONFIG: dict[str, float | int] = {
    "top_n": 35,
    "rebalance_freq": 90,
    "max_position_pct": 0.20,
    "warmup_days": 252,
    "test_warmup_days": 60,
    "min_stocks": 50,
    "min_history": 252,
    "stamp_duty": 0.0005,
    "commission": 0.00025,
    "slippage_base": 0.0005,
    "slippage_factor": 0.10,
    "risk_free_rate": 0.025,
    "n_folds": 5,
    "oos_pct": 0.20,
    "purge_days": 10,
    "max_per_sector": 5,
    "mdd_reduce_threshold": 0.10,
    "mdd_reduce_scale": 0.50,
    "mdd_stop_threshold": 0.18,
    "mdd_stop_scale": 0.25,
    "large_cap_exclude_pct": 0.20,
}

V35_WEIGHTS = {
    "rsi": 0.25,
    "bollinger": 0.25,
    "momentum": 0.20,
    "macd": 0.15,
    "vol_dev": 0.10,
    "low_vol": 0.05,
}


def _factor_rsi_mr(closes: np.ndarray) -> float:
    valid = closes[~np.isnan(closes)]
    if len(valid) < 15:
        return np.nan
    diff = np.diff(valid[-15:])
    gains = np.clip(diff, 0, None).mean()
    losses = -np.clip(diff, None, 0).mean()
    if losses < 1e-12:
        return np.nan
    return float(abs(100.0 - 100.0 / (1.0 + gains / losses) - 50.0))


def _factor_bollinger_mr(closes: np.ndarray) -> float:
    valid = closes[~np.isnan(closes)]
    if len(valid) < 20:
        return np.nan
    mean = valid[-20:].mean()
    std = valid[-20:].std(ddof=1)
    width = 4.0 * std
    return float(abs(valid[-1] - mean) / width) if width > 1e-12 else 0.0


def _factor_momentum(closes: np.ndarray) -> float:
    valid = closes[~np.isnan(closes)]
    if len(valid) < 68 or valid[-68] <= 0:
        return np.nan
    return float(valid[-1] / valid[-68] - 1.0)


def _ema(series: np.ndarray, span: int) -> float:
    valid = series[~np.isnan(series)]
    if len(valid) < span:
        return np.nan
    alpha = 2.0 / (span + 1)
    result = valid[:span].mean()
    for value in valid[span:]:
        result = alpha * value + (1 - alpha) * result
    return float(result)


def _factor_macd(closes: np.ndarray) -> float:
    valid = closes[~np.isnan(closes)]
    if len(valid) < 35:
        return np.nan
    return float(_ema(valid, 12) - _ema(valid, 26))


def _factor_vol_dev(volumes: np.ndarray) -> float:
    valid = volumes[~np.isnan(volumes)]
    if len(valid) < 20:
        return np.nan
    mean = valid[-20:].mean()
    return float(-abs(valid[-1] / mean - 1.0)) if mean > 1e-12 else 0.0


def _factor_low_vol(closes: np.ndarray) -> float:
    valid = closes[~np.isnan(closes)]
    if len(valid) < 61:
        return np.nan
    if np.any(valid[-61:-1] <= 0):
        return np.nan
    returns = np.diff(valid[-61:]) / valid[-61:-1]
    returns = returns[np.isfinite(returns)]
    if len(returns) < 20:
        return np.nan
    return float(-np.std(returns, ddof=1) * np.sqrt(252))


def _compute_factors(closes: np.ndarray, volumes: np.ndarray) -> dict[str, float]:
    return {
        "rsi": _factor_rsi_mr(closes),
        "bollinger": _factor_bollinger_mr(closes),
        "momentum": _factor_momentum(closes),
        "macd": _factor_macd(closes),
        "vol_dev": _factor_vol_dev(volumes),
        "low_vol": _factor_low_vol(closes),
    }


def _composite_score(
    factor_values: dict[str, float],
    weights: dict[str, float],
    cross_sectional_z: dict[str, tuple[float, float]],
) -> float:
    score = 0.0
    total_weight = 0.0
    for name, weight in weights.items():
        value = factor_values.get(name, np.nan)
        if np.isnan(value):
            continue
        if name in cross_sectional_z:
            mean, std = cross_sectional_z[name]
            if std > 1e-12:
                value = (value - mean) / std
        score += weight * value
        total_weight += weight
    return float(score / total_weight) if total_weight > 1e-12 else np.nan


def _load_aligned_data() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    files = sorted(DATA_DIR.glob("*.parquet"))
    if not files:
        raise RuntimeError(f"no parquet data found in {DATA_DIR}")
    close: dict[str, pd.Series] = {}
    volume: dict[str, pd.Series] = {}
    amount: dict[str, pd.Series] = {}
    turnover: dict[str, pd.Series] = {}
    for idx, file_path in enumerate(files, start=1):
        if idx % 300 == 0:
            print(f"  loaded {idx}/{len(files)} files", flush=True)
        df = pd.read_parquet(file_path)
        if len(df) < int(CONFIG["min_history"]):
            continue
        code = file_path.stem
        close[code] = df["close"].astype(float)
        volume[code] = df["volume"].astype(float)
        amount[code] = df["amount"].astype(float)
        turnover[code] = df["turnover"].astype(float)
    return (
        pd.DataFrame(close).sort_index(),
        pd.DataFrame(volume).sort_index(),
        pd.DataFrame(amount).sort_index(),
        pd.DataFrame(turnover).sort_index(),
    )


def _load_industries() -> dict[str, str]:
    try:
        df = pd.read_parquet(PROJECT_DIR / "data" / "industry_fixed.parquet")
    except Exception:
        return {}
    industry_map: dict[str, str] = {}
    for _, row in df.iterrows():
        code = str(row.get("code", ""))
        industry = row.get("industry", "")
        if not industry or pd.isna(industry):
            continue
        industry_map[code.split(".")[-1]] = str(industry)
    return industry_map


def _rank_stocks(snapshot: dict[str, dict[str, np.ndarray]]) -> list[tuple[str, float]]:
    raw_factors = {
        symbol: _compute_factors(data["close"], data["volume"]) for symbol, data in snapshot.items()
    }
    raw_factors = {
        symbol: factors
        for symbol, factors in raw_factors.items()
        if not all(np.isnan(value) for value in factors.values())
    }
    cross_z: dict[str, tuple[float, float]] = {}
    for name in V35_WEIGHTS:
        values = [factors.get(name, np.nan) for factors in raw_factors.values()]
        valid = [value for value in values if not np.isnan(value)]
        if len(valid) >= 5:
            cross_z[name] = (float(np.mean(valid)), float(np.std(valid, ddof=1)))
    ranked = []
    for symbol, factors in raw_factors.items():
        score = _composite_score(factors, V35_WEIGHTS, cross_z)
        if not np.isnan(score):
            ranked.append((symbol, float(score)))
    ranked.sort(key=lambda item: item[1], reverse=True)
    return ranked


def _select_with_sector_caps(
    ranked: list[tuple[str, float]],
    industry_map: dict[str, str],
    top_n: int,
    max_per_sector: int,
    excluded_large_caps: set[str],
) -> list[tuple[str, float]]:
    selected = []
    sector_counts: Counter[str] = Counter()
    for symbol, score in ranked:
        if symbol in excluded_large_caps:
            continue
        # Missing industry data must not collapse the whole universe into one
        # pseudo-sector, otherwise a sector cap of 5 silently becomes top-5.
        industry = industry_map.get(symbol) or f"__UNKNOWN__:{symbol}"
        if sector_counts[industry] >= max_per_sector:
            continue
        selected.append((symbol, score))
        sector_counts[industry] += 1
        if len(selected) >= top_n:
            break
    return selected


def _large_cap_exclusions(
    amount_window: pd.DataFrame,
    turnover_window: pd.DataFrame,
    exclude_pct: float,
) -> set[str]:
    if exclude_pct <= 0:
        return set()
    cap_proxy = amount_window.where(turnover_window > 1e-12) / turnover_window.where(
        turnover_window > 1e-12
    )
    latest_proxy = cap_proxy.tail(20).median(axis=0, skipna=True).dropna()
    if latest_proxy.empty:
        return set()
    n_exclude = max(1, int(len(latest_proxy) * exclude_pct))
    return set(latest_proxy.sort_values(ascending=False).head(n_exclude).index)


def _deduct_rebalance_cost(
    new_weights: dict[str, float],
    old_weights: dict[str, float],
    cfg: dict[str, float | int],
) -> tuple[float, float]:
    symbols = set(new_weights) | set(old_weights)
    buy_turnover = sum(max(new_weights.get(s, 0.0) - old_weights.get(s, 0.0), 0.0) for s in symbols)
    sell_turnover = sum(
        max(old_weights.get(s, 0.0) - new_weights.get(s, 0.0), 0.0) for s in symbols
    )
    turnover = buy_turnover + sell_turnover
    slippage = float(cfg["slippage_base"]) * (1 + float(cfg["slippage_factor"]) * np.sqrt(turnover))
    cost = (
        buy_turnover * float(cfg["commission"])
        + sell_turnover * (float(cfg["commission"]) + float(cfg["stamp_duty"]))
        + turnover * slippage
    )
    return float(cost), float(turnover)


def backtest(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    amount: pd.DataFrame,
    turnover: pd.DataFrame,
    industry_map: dict[str, str],
    start_idx: int,
    end_idx: int,
    cfg: dict[str, float | int],
    *,
    warmup: int | None = None,
    exclude_large_caps: bool,
) -> dict[str, Any] | None:
    warmup = int(warmup or cfg["warmup_days"])
    if end_idx - start_idx < warmup + 10:
        return None
    positions: dict[str, float] = {}
    daily_returns: list[float] = []
    turnovers: list[float] = []
    active_counts: list[int] = []
    candidate_counts: list[int] = []
    excluded_counts: list[int] = []
    equity = 1.0
    peak_equity = 1.0
    rebalance_cost = 0.0

    for t in range(start_idx + warmup, end_idx - 1):
        cost_today = 0.0
        if (t - start_idx - warmup) % int(cfg["rebalance_freq"]) == 0:
            window = slice(max(start_idx, t - 251), t + 1)
            close_window = close.iloc[window]
            volume_window = volume.iloc[window]
            amount_window = amount.iloc[window]
            turnover_window = turnover.iloc[window]
            latest = close.iloc[t].dropna()
            symbols = [symbol for symbol in latest.index if close_window[symbol].count() >= 60]
            snapshot = {
                symbol: {
                    "close": close_window[symbol].to_numpy(dtype=float),
                    "volume": volume_window[symbol].to_numpy(dtype=float),
                }
                for symbol in symbols
            }
            candidate_counts.append(len(snapshot))
            if len(snapshot) < int(cfg["min_stocks"]):
                positions = {}
                daily_returns.append(0.0)
                continue
            ranked = _rank_stocks(snapshot)
            excluded = (
                _large_cap_exclusions(
                    amount_window[list(snapshot)],
                    turnover_window[list(snapshot)],
                    float(cfg["large_cap_exclude_pct"]),
                )
                if exclude_large_caps
                else set()
            )
            selected = _select_with_sector_caps(
                ranked,
                industry_map,
                int(cfg["top_n"]),
                int(cfg["max_per_sector"]),
                excluded,
            )
            selected_symbols = {symbol for symbol, _ in selected}
            if not selected_symbols:
                positions = {}
                daily_returns.append(0.0)
                continue
            weight = min(1.0 / len(selected_symbols), float(cfg["max_position_pct"]))
            new_positions = {symbol: weight for symbol in selected_symbols}
            cost_today, day_turnover = _deduct_rebalance_cost(new_positions, positions, cfg)
            rebalance_cost += cost_today
            turnovers.append(day_turnover)
            active_counts.append(len(selected_symbols))
            excluded_counts.append(len(excluded))
            positions = new_positions

        current = close.iloc[t]
        nxt = close.iloc[t + 1]
        day_return = -cost_today
        for symbol, weight in positions.items():
            p0 = current.get(symbol, np.nan)
            p1 = nxt.get(symbol, np.nan)
            if pd.isna(p0) or pd.isna(p1) or p0 <= 0 or p1 <= 0:
                continue
            day_return += weight * (float(p1) / float(p0) - 1.0)
        daily_returns.append(day_return)
        equity *= 1.0 + day_return
        peak_equity = max(peak_equity, equity)
        current_dd = (equity - peak_equity) / peak_equity
        if current_dd < -float(cfg["mdd_stop_threshold"]):
            positions = {
                symbol: weight * float(cfg["mdd_stop_scale"])
                for symbol, weight in positions.items()
            }
        elif current_dd < -float(cfg["mdd_reduce_threshold"]):
            positions = {
                symbol: weight * float(cfg["mdd_reduce_scale"])
                for symbol, weight in positions.items()
            }

    if len(daily_returns) < 50:
        return None
    returns = np.array(daily_returns, dtype=float)
    ann_return = float(np.mean(returns) * 252)
    ann_vol = float(np.std(returns, ddof=1) * np.sqrt(252))
    sharpe = (ann_return - float(cfg["risk_free_rate"])) / ann_vol if ann_vol > 1e-8 else None
    equity_curve = np.cumprod(1 + returns)
    peak = np.maximum.accumulate(equity_curve)
    max_dd = float(np.min((equity_curve - peak) / peak))
    return {
        "sharpe_ratio": (
            round(float(sharpe), 4) if sharpe is not None and abs(sharpe) < 100 else None
        ),
        "annual_return": round(ann_return, 4),
        "annual_volatility": round(ann_vol, 4),
        "max_drawdown": round(max_dd, 4),
        "calmar_ratio": round(ann_return / abs(max_dd), 4) if abs(max_dd) > 1e-10 else 0.0,
        "win_rate": round(float(np.mean(returns > 0)), 4),
        "total_return": round(float(equity_curve[-1] - 1), 4),
        "n_rebalances": len(turnovers),
        "n_days": len(daily_returns),
        "avg_active_stocks": round(float(np.mean(active_counts)), 1) if active_counts else 0.0,
        "avg_candidate_stocks": (
            round(float(np.mean(candidate_counts)), 1) if candidate_counts else 0.0
        ),
        "avg_excluded_large_caps": (
            round(float(np.mean(excluded_counts)), 1) if excluded_counts else 0.0
        ),
        "avg_turnover": round(float(np.mean(turnovers)), 4) if turnovers else 0.0,
        "total_rebalance_cost": round(rebalance_cost, 4),
    }


def walk_forward(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    amount: pd.DataFrame,
    turnover: pd.DataFrame,
    industry_map: dict[str, str],
    cfg: dict[str, float | int],
    *,
    exclude_large_caps: bool,
) -> dict[str, Any]:
    n = len(close)
    oos_total = int(n * float(cfg["oos_pct"]))
    fold_size = oos_total // int(cfg["n_folds"])
    first_test_start = n - oos_total
    purge = int(cfg["purge_days"])
    folds = []
    is_sharpes = []
    oos_sharpes = []
    for fold in range(int(cfg["n_folds"])):
        test_start = first_test_start + fold * fold_size
        test_end = min(test_start + fold_size, n)
        train_end = test_start - purge
        train = backtest(
            close,
            volume,
            amount,
            turnover,
            industry_map,
            0,
            train_end,
            cfg,
            exclude_large_caps=exclude_large_caps,
        )
        oos = backtest(
            close,
            volume,
            amount,
            turnover,
            industry_map,
            train_end,
            test_end,
            cfg,
            warmup=int(cfg["test_warmup_days"]),
            exclude_large_caps=exclude_large_caps,
        )
        row = {
            "fold": fold,
            "train_days": train_end,
            "test_days": test_end - test_start,
            "is": train["sharpe_ratio"] if train else None,
            "oos": oos["sharpe_ratio"] if oos else None,
            "active": oos["avg_active_stocks"] if oos else None,
        }
        if row["is"] is not None:
            is_sharpes.append(float(row["is"]))
        if row["oos"] is not None:
            oos_sharpes.append(float(row["oos"]))
        folds.append(row)
        print(f"  F{fold}: IS={row['is']} OOS={row['oos']} active={row['active']}", flush=True)
    avg_is = float(np.mean(is_sharpes)) if is_sharpes else np.nan
    avg_oos = float(np.mean(oos_sharpes)) if oos_sharpes else np.nan
    decay = (avg_is - avg_oos) / abs(avg_is) if abs(avg_is) > 1e-8 else np.nan
    return {
        "folds": folds,
        "avg_is_sharpe": round(avg_is, 4) if not np.isnan(avg_is) else None,
        "avg_oos_sharpe": round(avg_oos, 4) if not np.isnan(avg_oos) else None,
        "sharpe_decay": round(decay, 4) if not np.isnan(decay) else None,
    }


def _gates(full: dict[str, Any] | None, wf: dict[str, Any]) -> dict[str, bool]:
    return {
        "S": bool(full and full["sharpe_ratio"] is not None and full["sharpe_ratio"] >= 1.2),
        "M": bool(full and full["max_drawdown"] >= -0.15),
        "D": bool(wf["sharpe_decay"] is not None and wf["sharpe_decay"] <= 0.30),
        "W": bool(full and full["win_rate"] >= 0.40),
    }


def _run_scenario(
    name: str,
    close: pd.DataFrame,
    volume: pd.DataFrame,
    amount: pd.DataFrame,
    turnover: pd.DataFrame,
    industry_map: dict[str, str],
    *,
    exclude_large_caps: bool,
) -> dict[str, Any]:
    print(f"\n{name}: full backtest", flush=True)
    full = backtest(
        close,
        volume,
        amount,
        turnover,
        industry_map,
        0,
        len(close),
        CONFIG,
        exclude_large_caps=exclude_large_caps,
    )
    print(f"{name}: walk-forward", flush=True)
    wf = walk_forward(
        close,
        volume,
        amount,
        turnover,
        industry_map,
        CONFIG,
        exclude_large_caps=exclude_large_caps,
    )
    gates = _gates(full, wf)
    gates["A"] = all(gates.values())
    return {"full": full, "wf": wf, "gates": gates}


def main() -> None:
    started = time.time()
    print("Loading aligned 25-year data...", flush=True)
    close, volume, amount, turnover = _load_aligned_data()
    industry_map = _load_industries()
    report: dict[str, Any] = {
        "ts": datetime.now().isoformat(),
        "version": "V5.9-aligned-validation",
        "description": (
            "Date-aligned V5.9 validation with cost deduction and optional "
            "large-cap exclusion via amount/turnover proxy"
        ),
        "data": {
            "symbols": int(close.shape[1]),
            "dates": int(close.shape[0]),
            "start": str(close.index.min().date()),
            "end": str(close.index.max().date()),
            "industry_mapped": len(industry_map),
            "industry_coverage": round(
                len(set(close.columns) & set(industry_map)) / close.shape[1], 4
            ),
            "sector_cap_note": (
                "Unknown industries are treated as per-symbol pseudo-sectors to avoid "
                "collapsing the portfolio to max_per_sector holdings."
            ),
        },
        "config": CONFIG,
        "scenarios": {},
    }
    print(
        f"  symbols={close.shape[1]} dates={close.shape[0]} "
        f"{report['data']['start']}->{report['data']['end']}",
        flush=True,
    )
    report["scenarios"]["baseline_aligned_cost_aware"] = _run_scenario(
        "baseline_aligned_cost_aware",
        close,
        volume,
        amount,
        turnover,
        industry_map,
        exclude_large_caps=False,
    )
    report["scenarios"]["exclude_top_20pct_large_cap_proxy"] = _run_scenario(
        "exclude_top_20pct_large_cap_proxy",
        close,
        volume,
        amount,
        turnover,
        industry_map,
        exclude_large_caps=True,
    )
    report["elapsed_s"] = round(time.time() - started, 1)
    out_path = RESULTS_DIR / "quant_logic_validation_v5_9_aligned_ex_large_cap.json"
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"\nWrote {out_path}", flush=True)
    for name, result in report["scenarios"].items():
        full = result["full"] or {}
        wf = result["wf"] or {}
        gates = result["gates"]
        print(
            f"{name}: Sharpe={full.get('sharpe_ratio')} "
            f"MDD={full.get('max_drawdown')} Win={full.get('win_rate')} "
            f"OOS={wf.get('avg_oos_sharpe')} Decay={wf.get('sharpe_decay')} "
            f"Gates={gates}",
            flush=True,
        )


if __name__ == "__main__":
    main()
