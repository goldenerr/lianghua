#!/usr/bin/env python3
"""Research V12: V11 defensive overlay plus point-in-time industry rotation.

This research candidate targets the V9/V10/V11 weak spot: alpha instability in
specific market regimes. It adds a point-in-time industry whitelist and industry
score blend on top of stock-level dynamic IC/fundamental ranks.
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
import validate_quant_logic_v5_9 as base
from _paths import PROJECT_DIR, RESULTS_DIR
from research_fundamental_quality_v7 import _load_fundamentals

RESULTS_DIR.mkdir(parents=True, exist_ok=True)

CONFIG: dict[str, float | int | str] = deepcopy(v9.CONFIG)
CONFIG.update(
    {
        "top_n": 40,
        "rebalance_freq": 10,
        "target_vol": 0.12,
        "gross_exposure": 0.75,
        "max_position_pct": 0.025,
        "hedge_asset": "idx_000905",
        "normal_hedge_weight": 0.10,
        "bear_hedge_weight": 0.35,
        "hedge_cost": 0.00008,
        "trend_lookback": 120,
        "trend_vol_lookback": 60,
        "trend_sleeve_weight": 0.30,
        "bear_trend_sleeve_weight": 0.45,
        "trend_cost": 0.00025,
        "industry_lookback": 120,
        "industry_vol_lookback": 60,
        "industry_rotation_weight": 0.35,
        "industry_top_normal": 16,
        "industry_top_bear": 8,
        "industry_min_members": 3,
        "industry_min_score": 0.0,
        "industry_filter_min_symbols": 80,
        "industry_risk_off_scale": 0.0,
    }
)

BROAD_TREND_ASSETS = {
    "hs300": "etf_510300",
    "csi500": "etf_510500",
    "csi1000": "etf_512100",
    "chinext": "etf_159915",
    "star50": "etf_588000",
    "cash": "etf_511880",
    "gold": "etf_518880",
}

DEFENSIVE_TREND_ASSETS = {
    "cash": "etf_511880",
    "gold": "etf_518880",
}


def _trend_assets_from_env() -> tuple[str, dict[str, str]]:
    mode = os.environ.get("QUANT_V11_TREND_ASSET_MODE", "defensive").strip().lower()
    if mode in {"defensive", "cash_gold", "cash-gold"}:
        return "defensive", DEFENSIVE_TREND_ASSETS
    if mode in {"broad", "all", "all_etf", "all-etf"}:
        return "broad", BROAD_TREND_ASSETS
    raise ValueError(
        "QUANT_V11_TREND_ASSET_MODE must be one of: defensive, cash_gold, broad, all_etf"
    )


TREND_ASSET_MODE, TREND_ASSETS = _trend_assets_from_env()


def _load_hedge_returns(index: pd.DatetimeIndex, asset_id: str) -> pd.Series:
    path = PROJECT_DIR / "data" / "benchmarks" / f"{asset_id}.parquet"
    if not path.exists():
        raise RuntimeError(f"missing hedge benchmark data: {path}")
    df = pd.read_parquet(path)
    return df["close"].astype(float).pct_change(fill_method=None).reindex(index).fillna(0.0)


def _load_trend_prices(index: pd.DatetimeIndex) -> pd.DataFrame:
    prices = {}
    for name, asset_id in TREND_ASSETS.items():
        path = PROJECT_DIR / "data" / "benchmarks" / f"{asset_id}.parquet"
        if not path.exists():
            continue
        df = pd.read_parquet(path)
        prices[name] = df["close"].astype(float)
    if "cash" not in prices:
        prices["cash"] = pd.Series(1.0, index=index)
    return pd.DataFrame(prices).reindex(index).ffill()


def _trend_overlay_weights(
    prices: pd.DataFrame,
    t: int,
    residual: float,
    bear: bool,
    cfg: dict[str, float | int | str],
) -> dict[str, float]:
    lookback = int(cfg["trend_lookback"])
    vol_lookback = int(cfg["trend_vol_lookback"])
    sleeve = residual * (
        float(cfg["bear_trend_sleeve_weight"]) if bear else float(cfg["trend_sleeve_weight"])
    )
    weights = {"cash": residual}
    if t < max(lookback, vol_lookback) or sleeve <= 0:
        return weights
    latest = prices.iloc[t]
    past = prices.iloc[t - lookback].reindex(latest.index)
    momentum = latest / past.replace(0, np.nan) - 1.0
    vol = prices.pct_change(fill_method=None).iloc[t - vol_lookback : t].std().replace(0, np.nan)
    score = (momentum / vol).replace([np.inf, -np.inf], np.nan).dropna()
    score = score[score.index != "cash"]
    positive = score[score > 0].sort_values(ascending=False)
    if positive.empty:
        gold_mom = momentum.get("gold", np.nan)
        if pd.notna(gold_mom) and gold_mom > 0:
            weights["gold"] = sleeve
            weights["cash"] = residual - sleeve
        return weights
    selected = list(positive.head(2).index)
    each = sleeve / len(selected)
    weights = {"cash": residual - sleeve}
    for asset in selected:
        weights[str(asset)] = each
    return weights


def _industry_rotation_scores(
    close: pd.DataFrame,
    industry_map: dict[str, str],
    t: int,
    cfg: dict[str, float | int | str],
) -> pd.Series:
    lookback = int(cfg["industry_lookback"])
    vol_lookback = int(cfg["industry_vol_lookback"])
    if t < max(lookback, vol_lookback):
        return pd.Series(dtype=float)
    latest = close.iloc[t].dropna()
    past = close.iloc[t - lookback].reindex(latest.index)
    momentum = latest / past.replace(0, np.nan) - 1.0
    returns = close.pct_change(fill_method=None).iloc[t - vol_lookback : t]
    volatility = returns.reindex(columns=latest.index).std().replace(0, np.nan)
    industry = pd.Series(
        {
            symbol: industry_map.get(str(symbol), f"__UNKNOWN__:{symbol}")
            for symbol in latest.index
        },
        dtype=object,
    )
    frame = pd.DataFrame(
        {
            "industry": industry,
            "momentum": momentum,
            "volatility": volatility,
        }
    ).replace([np.inf, -np.inf], np.nan)
    frame = frame.dropna(subset=["industry", "momentum", "volatility"])
    if frame.empty:
        return pd.Series(dtype=float)
    grouped = frame.groupby("industry").agg(
        momentum=("momentum", "median"),
        volatility=("volatility", "median"),
        members=("momentum", "count"),
    )
    grouped = grouped[grouped["members"] >= int(cfg["industry_min_members"])]
    if grouped.empty:
        return pd.Series(dtype=float)
    score = grouped["momentum"] / grouped["volatility"].replace(0, np.nan)
    return score.replace([np.inf, -np.inf], np.nan).dropna()


def _apply_industry_rotation(
    score: pd.Series,
    industry_scores: pd.Series,
    industry_map: dict[str, str],
    bear: bool,
    cfg: dict[str, float | int | str],
) -> pd.Series:
    if score.empty or industry_scores.empty:
        return score
    count = int(cfg["industry_top_bear"] if bear else cfg["industry_top_normal"])
    positive = industry_scores[industry_scores > float(cfg["industry_min_score"])]
    ranked = positive.sort_values(ascending=False)
    if ranked.empty and not bear:
        ranked = industry_scores.sort_values(ascending=False)
    allowed = set(ranked.head(count).index)
    if not allowed:
        return score.iloc[0:0] if bear else score
    industries = pd.Series(
        {
            symbol: industry_map.get(str(symbol), f"__UNKNOWN__:{symbol}")
            for symbol in score.index
        }
    )
    industry_factor = industries.map(industry_scores).astype(float)
    blend = (
        (1.0 - float(cfg["industry_rotation_weight"])) * v9._safe_z(score)
        + float(cfg["industry_rotation_weight"]) * v9._safe_z(industry_factor)
    ).dropna()
    filtered = blend[industries.reindex(blend.index).isin(allowed)]
    if len(filtered) >= int(cfg["industry_filter_min_symbols"]) or bear:
        return filtered
    return blend


def _hedged_backtest(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    amount: pd.DataFrame,
    turnover: pd.DataFrame,
    fundamentals: dict[str, pd.DataFrame],
    industry_map: dict[str, str],
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
    trend_prices = _load_trend_prices(close.index)
    trend_returns = trend_prices.pct_change(fill_method=None).fillna(0.0)
    hedge_returns = _load_hedge_returns(close.index, str(cfg["hedge_asset"]))
    positions: dict[str, float] = {}
    overlay_weights: dict[str, float] = {"cash": 0.0}
    hedge_weight = 0.0
    returns: list[float] = []
    turnovers: list[float] = []
    hedge_turnovers: list[float] = []
    trend_turnovers: list[float] = []
    active_counts: list[int] = []
    equity = 1.0
    peak_equity = 1.0
    total_cost = 0.0
    exposure_values: list[float] = []
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
                fundamentals, symbols, date, int(base_cfg["report_lag_days"])
            )
            fund_score = v9._score(snapshot, latest)
            combined = (
                float(base_cfg["technical_weight"]) * v9._safe_z(tech_score)
                + float(base_cfg["fundamental_weight"]) * v9._safe_z(fund_score)
            ).dropna()
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
                industry_scores = _industry_rotation_scores(close, industry_map, t, cfg)
                combined = _apply_industry_rotation(
                    combined, industry_scores, industry_map, bear, cfg
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
            new_overlay = _trend_overlay_weights(trend_prices, t, residual, bear, cfg)
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
            close, volume, amount, turnover, fundamentals, industry_map, 0, train_end, cfg
        )
        context_start = max(0, test_start - int(cfg["warmup_days"]))
        oos = _hedged_backtest(
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


def _gates(full: dict[str, Any] | None, wf: dict[str, Any]) -> dict[str, bool]:
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
        f"{close.index.min().date()}->{close.index.max().date()}",
        flush=True,
    )
    full = _hedged_backtest(close, volume, amount, turnover, fundamentals, industry_map, 0, len(close), CONFIG)
    print(f"full={full}", flush=True)
    wf = _walk_forward(close, volume, amount, turnover, fundamentals, industry_map, CONFIG)
    report = {
        "ts": datetime.now().isoformat(),
        "version": "V12-industry-rotation-defensive-overlay",
        "research_only": True,
        "trend_asset_mode": TREND_ASSET_MODE,
        "trend_assets": TREND_ASSETS,
        "production_blockers": [
            "requires point-in-time production industry provider evidence",
            "requires exchange-backed futures provider",
            "requires futures margin and rollover approval evidence",
            "requires position reconciliation for hedge leg",
            "requires approved ETF execution and liquidity/capacity evidence",
            "requires paper-trading and small-live evidence before production",
        ],
        "config": CONFIG,
        "full": full,
        "wf": wf,
        "gates": _gates(full, wf),
        "elapsed_s": round(time.time() - started, 1),
    }
    out_path = RESULTS_DIR / "quant_logic_research_v12_industry_rotation.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {out_path}")
    print(f"gates={report['gates']}")


if __name__ == "__main__":
    main()
