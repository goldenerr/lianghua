#!/usr/bin/env python3
"""Research V17: alternative-data expert plus crisis-alpha sleeve.

This is a strict research-only extension of V16. It consumes the V17 feature
store, uses alternative data only when rows are available before the rebalance
date, and keeps sparse snapshot coverage visible in the output instead of
pretending it is a full historical alpha source.
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
import research_moe_router_v16 as v16
import validate_quant_logic_v5_9 as base
from _paths import ALT_FEATURES_DIR, RESULTS_DIR
from research_fundamental_quality_v7 import _load_fundamentals

RESULTS_DIR.mkdir(parents=True, exist_ok=True)

CONFIG: dict[str, float | int | str] = deepcopy(v16.CONFIG)
CONFIG.update(
    {
        "normal_technical_weight": 0.40,
        "normal_fundamental_weight": 0.30,
        "normal_earnings_weight": 0.10,
        "normal_defensive_weight": 0.10,
        "normal_alt_weight": 0.10,
        "bear_technical_weight": 0.10,
        "bear_fundamental_weight": 0.30,
        "bear_earnings_weight": 0.15,
        "bear_defensive_weight": 0.25,
        "bear_alt_weight": 0.20,
        "alt_lag_days": 1,
        "min_alt_coverage": 40,
        "alt_analyst_weight": 0.25,
        "alt_revision_weight": 0.25,
        "alt_northbound_weight": 0.20,
        "alt_flow_weight": 0.15,
        "alt_liquidity_weight": 0.15,
        "crisis_sleeve_weight": 0.10,
        "bear_crisis_sleeve_weight": 0.25,
        "crisis_lookback": 60,
        "crisis_vol_lookback": 20,
        "crisis_max_assets": 3,
        "crisis_cost": 0.00035,
        "crisis_include_futures_shorts": 1,
        "crisis_short_fraction": 0.35,
        "bear_reentry_enabled": 0,
        "bear_reentry_lookback": 20,
        "bear_reentry_momentum_threshold": 0.02,
        "bear_reentry_breadth_threshold": 0.55,
        "bear_reentry_exposure": 0.45,
        "bear_reentry_treat_as_normal": 1,
    }
)

RECENT_METRICS_START = os.environ.get("QUANT_V17_RECENT_METRICS_START", "2024-01-01")
STOCK_ALT_FEATURE_FILE = os.environ.get(
    "QUANT_V17_STOCK_ALT_FEATURE_FILE", "v17_stock_alt_features.parquet"
)
CRISIS_ALT_FEATURE_FILE = os.environ.get(
    "QUANT_V17_CRISIS_ALT_FEATURE_FILE", "v17_crisis_asset_features.parquet"
)
ALT_FEATURE_SUMMARY_FILE = os.environ.get("QUANT_V17_ALT_FEATURE_SUMMARY_FILE", "").strip()
OUTPUT_SUFFIX = os.environ.get("QUANT_V17_OUTPUT_SUFFIX", "").strip()
REQUIRE_ARCHIVE_COMPLETE_FOR_FINALIST = (
    os.environ.get("QUANT_V17_REQUIRE_ARCHIVE_COMPLETE", "1") == "1"
)


def _output_path(base_name: str) -> Any:
    if not OUTPUT_SUFFIX:
        return RESULTS_DIR / base_name
    safe_suffix = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in OUTPUT_SUFFIX)
    stem, dot, ext = base_name.rpartition(".")
    if not dot:
        return RESULTS_DIR / f"{base_name}_{safe_suffix}"
    return RESULTS_DIR / f"{stem}_{safe_suffix}.{ext}"


def _read_features(path: str) -> pd.DataFrame:
    feature_path = ALT_FEATURES_DIR / path
    if not feature_path.exists():
        return pd.DataFrame()
    return pd.read_parquet(feature_path)


def _summary_file_name() -> str | None:
    if ALT_FEATURE_SUMMARY_FILE:
        return ALT_FEATURE_SUMMARY_FILE
    suffix = "_stock_alt_features.parquet"
    if STOCK_ALT_FEATURE_FILE.endswith(suffix):
        return STOCK_ALT_FEATURE_FILE.removesuffix(suffix) + "_alt_feature_summary.json"
    return None


def _load_alt_feature_summary() -> dict[str, Any]:
    file_name = _summary_file_name()
    if not file_name:
        return {}
    path = ALT_FEATURES_DIR / file_name
    if not path.exists():
        return {"summary_file": file_name, "summary_missing": True}
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["summary_file"] = file_name
    return payload


def _load_alt_features(symbols: pd.Index) -> pd.DataFrame:
    df = _read_features(STOCK_ALT_FEATURE_FILE)
    if df.empty:
        return df
    out = df.copy()
    out["code"] = out["code"].astype(str).str.zfill(6)
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.normalize()
    out = out[out["code"].isin({str(symbol).zfill(6) for symbol in symbols})]
    out = out.dropna(subset=["code", "date"]).sort_values(["date", "code"])
    return out.drop_duplicates(["date", "code"], keep="last")


def _load_crisis_prices(index: pd.DatetimeIndex) -> tuple[pd.DataFrame, dict[str, str]]:
    df = _read_features(CRISIS_ALT_FEATURE_FILE)
    if df.empty:
        return pd.DataFrame(index=index), {}
    working = df.copy()
    working["date"] = pd.to_datetime(working["date"], errors="coerce").dt.normalize()
    working = working.dropna(subset=["date", "asset_id", "close"])
    roles = {
        str(row.asset_id): str(row.role)
        for row in working[["asset_id", "role"]].drop_duplicates().itertuples(index=False)
    }
    prices = working.pivot_table(index="date", columns="asset_id", values="close", aggfunc="last")
    prices = prices.sort_index().reindex(index).ffill()
    return prices, roles


def _safe_z(series: pd.Series) -> pd.Series:
    return v15._event_z(series)


def _latest_alt_snapshot(
    alt_features: pd.DataFrame,
    date: pd.Timestamp,
    symbols: list[str],
    cfg: dict[str, float | int | str],
) -> pd.DataFrame:
    if alt_features.empty:
        return pd.DataFrame(index=symbols)
    cutoff = date - pd.Timedelta(days=int(cfg["alt_lag_days"]))
    rows = alt_features[alt_features["date"] <= cutoff]
    if rows.empty:
        return pd.DataFrame(index=symbols)
    snapshot = rows.sort_values(["date", "code"]).drop_duplicates("code", keep="last")
    return snapshot.set_index("code").reindex(symbols)


def _num(snapshot: pd.DataFrame, column: str) -> pd.Series:
    if column not in snapshot.columns:
        return pd.Series(index=snapshot.index, dtype=float)
    return pd.to_numeric(snapshot[column], errors="coerce")


def _alt_data_score(
    alt_features: pd.DataFrame,
    date: pd.Timestamp,
    symbols: list[str],
    cfg: dict[str, float | int | str],
) -> tuple[pd.Series, int]:
    snapshot = _latest_alt_snapshot(alt_features, date, symbols, cfg)
    if snapshot.empty:
        return pd.Series(dtype=float), 0
    analyst = (
        0.30 * _safe_z(_num(snapshot, "analyst_positive_rating_ratio")).fillna(0.0)
        - 0.15 * _safe_z(_num(snapshot, "analyst_negative_rating_ratio")).fillna(0.0)
        + 0.35 * _safe_z(_num(snapshot, "analyst_eps_growth_26_vs_25")).fillna(0.0)
        + 0.20 * _safe_z(_num(snapshot, "analyst_report_count_6m")).fillna(0.0)
    )
    revision_mode = str(cfg.get("alt_revision_mode", "balanced"))
    if (
        revision_mode == "stable_rating_60d"
        and "analyst_rating_score_mean_60d" in snapshot.columns
    ):
        revision = _safe_z(_num(snapshot, "analyst_rating_score_mean_60d")).fillna(0.0)
    elif "analyst_rating_score_mean_60d" in snapshot.columns:
        revision = (
            0.35 * _safe_z(_num(snapshot, "analyst_rating_score_mean_60d")).fillna(0.0)
            + 0.25 * _safe_z(_num(snapshot, "analyst_revision_sum_60d")).fillna(0.0)
            - 0.20 * _safe_z(_num(snapshot, "analyst_event_count_20d")).fillna(0.0)
            - 0.20 * _safe_z(_num(snapshot, "analyst_event_count_60d")).fillna(0.0)
        )
    else:
        revision = (
            0.40 * _safe_z(_num(snapshot, "analyst_revision_score_sum")).fillna(0.0)
            + 0.40 * _safe_z(_num(snapshot, "analyst_upgrade_minus_downgrade")).fillna(0.0)
            + 0.20 * _safe_z(_num(snapshot, "analyst_rating_score_mean")).fillna(0.0)
        )
    northbound = (
        0.40 * _safe_z(_num(snapshot, "northbound_hold_pct_change_5d")).fillna(0.0)
        + 0.35 * _safe_z(_num(snapshot, "northbound_net_buy_amount")).fillna(0.0)
        + 0.25 * _safe_z(_num(snapshot, "northbound_hold_pct")).fillna(0.0)
    )
    flow = (
        0.35 * _safe_z(_num(snapshot, "fund_flow_net_3d")).fillna(0.0)
        + 0.25 * _safe_z(_num(snapshot, "fund_flow_net_5d")).fillna(0.0)
        + 0.25 * _safe_z(_num(snapshot, "big_deal_buy_sell_imbalance")).fillna(0.0)
        - 0.15 * _safe_z(_num(snapshot, "margin_short_sell_volume")).fillna(0.0)
    )
    liquidity = (
        0.30 * _safe_z(_num(snapshot, "intraday_reversal_alpha")).fillna(0.0)
        - 0.25 * _safe_z(_num(snapshot, "realized_vol_5m")).fillna(0.0)
        - 0.20 * _safe_z(_num(snapshot, "amihud_5m")).fillna(0.0)
        + 0.15 * _safe_z(_num(snapshot, "close_vs_vwap")).fillna(0.0)
        + 0.10 * _safe_z(_num(snapshot, "last_hour_volume_share")).fillna(0.0)
    )
    raw = (
        float(cfg["alt_analyst_weight"]) * _safe_z(analyst).fillna(0.0)
        + float(cfg["alt_revision_weight"]) * _safe_z(revision).fillna(0.0)
        + float(cfg["alt_northbound_weight"]) * _safe_z(northbound).fillna(0.0)
        + float(cfg["alt_flow_weight"]) * _safe_z(flow).fillna(0.0)
        + float(cfg["alt_liquidity_weight"]) * _safe_z(liquidity).fillna(0.0)
    )
    coverage = int(raw.replace([np.inf, -np.inf], np.nan).notna().sum())
    if coverage < int(cfg["min_alt_coverage"]):
        return pd.Series(dtype=float), coverage
    return _safe_z(raw).dropna(), coverage


def _router_weights(bear: bool, cfg: dict[str, float | int | str]) -> dict[str, float]:
    prefix = "bear" if bear else "normal"
    weights = {
        "technical": float(cfg[f"{prefix}_technical_weight"]),
        "fundamental": float(cfg[f"{prefix}_fundamental_weight"]),
        "earnings": float(cfg[f"{prefix}_earnings_weight"]),
        "defensive": float(cfg[f"{prefix}_defensive_weight"]),
        "alt": float(cfg[f"{prefix}_alt_weight"]),
    }
    total = sum(max(value, 0.0) for value in weights.values())
    if total <= 0:
        return {
            "technical": 0.4,
            "fundamental": 0.3,
            "earnings": 0.1,
            "defensive": 0.1,
            "alt": 0.1,
        }
    return {name: max(value, 0.0) / total for name, value in weights.items()}


def _bear_reentry_signal(
    close_window: pd.DataFrame,
    cfg: dict[str, float | int | str],
) -> bool:
    """Detect short-term breadth rebound using only data available at rebalance time."""
    if not int(cfg.get("bear_reentry_enabled", 0)):
        return False
    lookback = int(cfg.get("bear_reentry_lookback", 20))
    if len(close_window) < lookback + 1:
        return False
    latest = close_window.iloc[-1].dropna()
    past = close_window.iloc[-lookback - 1].reindex(latest.index)
    momentum = (latest / past.replace(0, np.nan) - 1.0).median(skipna=True)
    breadth = (latest > close_window.tail(lookback).mean(skipna=True).reindex(latest.index)).mean()
    return bool(
        pd.notna(momentum)
        and float(momentum) >= float(cfg.get("bear_reentry_momentum_threshold", 0.02))
        and float(breadth) >= float(cfg.get("bear_reentry_breadth_threshold", 0.55))
    )


def _combine_v17_experts(
    technical: pd.Series,
    fundamental: pd.Series,
    earnings: pd.Series,
    defensive: pd.Series,
    alt: pd.Series,
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
        "alt": alt.reindex(symbols),
    }
    coverage_floor = {
        "technical": int(cfg["min_expert_coverage"]),
        "fundamental": int(cfg["min_expert_coverage"]),
        "earnings": int(cfg["min_expert_coverage"]),
        "defensive": int(cfg["min_expert_coverage"]),
        "alt": int(cfg["min_alt_coverage"]),
    }
    combined = pd.Series(0.0, index=symbols, dtype=float)
    total = pd.Series(0.0, index=symbols, dtype=float)
    for name, score in scores.items():
        z = _safe_z(score)
        valid = z.notna()
        if int(valid.sum()) < coverage_floor[name]:
            continue
        combined.loc[valid] += weights[name] * z.loc[valid]
        total.loc[valid] += weights[name]
    result = (combined / total.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)
    return _safe_z(result).dropna()


def _return_stats(returns: np.ndarray, risk_free_rate: float) -> dict[str, Any]:
    if len(returns) < 50:
        return {}
    ann_return = float(np.mean(returns) * 252)
    ann_vol = float(np.std(returns, ddof=1) * np.sqrt(252))
    sharpe = (ann_return - risk_free_rate) / ann_vol if ann_vol > 1e-8 else None
    curve = np.cumprod(1 + returns)
    peak = np.maximum.accumulate(curve)
    max_dd = float(np.min((curve - peak) / peak))
    return {
        "sharpe_ratio": round(float(sharpe), 4) if sharpe is not None else None,
        "annual_return": round(ann_return, 4),
        "annual_volatility": round(ann_vol, 4),
        "max_drawdown": round(max_dd, 4),
        "win_rate": round(float(np.mean(returns > 0)), 4),
        "total_return": round(float(curve[-1] - 1), 4),
        "n_days": int(len(returns)),
    }


def _crisis_overlay_weights(
    crisis_prices: pd.DataFrame,
    roles: dict[str, str],
    t: int,
    residual: float,
    bear: bool,
    cfg: dict[str, float | int | str],
) -> dict[str, float]:
    sleeve = residual * (
        float(cfg["bear_crisis_sleeve_weight"]) if bear else float(cfg["crisis_sleeve_weight"])
    )
    if sleeve <= 0 or crisis_prices.empty:
        return {}
    lookback = int(cfg["crisis_lookback"])
    vol_lookback = int(cfg["crisis_vol_lookback"])
    if t < max(lookback, vol_lookback):
        return {}
    latest = crisis_prices.iloc[t].dropna()
    past = crisis_prices.iloc[t - lookback].reindex(latest.index)
    returns = crisis_prices.pct_change(fill_method=None)
    momentum = latest / past.replace(0, np.nan) - 1.0
    vol = returns.iloc[t - vol_lookback : t].std().replace(0, np.nan)
    score = (momentum / vol).replace([np.inf, -np.inf], np.nan).dropna()
    if score.empty:
        return {}
    defensive_roles = {"cash_defensive", "bond_defensive", "duration_defensive", "gold_crisis_alpha"}
    if bear:
        preferred = score[score.index.map(lambda asset: roles.get(str(asset)) in defensive_roles)]
        if not preferred.empty:
            score = 0.65 * _safe_z(score).fillna(0.0) + 0.35 * _safe_z(preferred.reindex(score.index)).fillna(0.0)
    positive = score[score > 0].sort_values(ascending=False)
    weights: dict[str, float] = {}
    long_sleeve = sleeve
    if bool(int(cfg["crisis_include_futures_shorts"])) and bear:
        futures_negative = score[
            score.index.map(lambda asset: str(asset).startswith("fut_")) & (score < 0)
        ].sort_values()
        if not futures_negative.empty:
            short_sleeve = sleeve * float(cfg["crisis_short_fraction"])
            long_sleeve = max(0.0, sleeve - short_sleeve)
            selected_short = list(futures_negative.head(max(1, int(cfg["crisis_max_assets"]) // 2)).index)
            each_short = -short_sleeve / len(selected_short)
            for asset in selected_short:
                weights[str(asset)] = each_short
    if positive.empty:
        return weights
    selected = list(positive.head(int(cfg["crisis_max_assets"])).index)
    each = long_sleeve / len(selected)
    for asset in selected:
        weights[str(asset)] = weights.get(str(asset), 0.0) + each
    return weights


def _merge_overlay_weights(
    trend_weights: dict[str, float],
    crisis_weights: dict[str, float],
) -> dict[str, float]:
    if not crisis_weights:
        return trend_weights
    merged = dict(trend_weights)
    crisis_abs = sum(abs(weight) for weight in crisis_weights.values())
    merged["cash"] = max(0.0, merged.get("cash", 0.0) - crisis_abs)
    for asset, weight in crisis_weights.items():
        merged[asset] = merged.get(asset, 0.0) + weight
    return merged


def _hedged_backtest_v17(
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
    crisis_returns = crisis_prices.pct_change(fill_method=None).reindex(close.index).fillna(0.0)
    overlay_returns = pd.concat([trend_returns, crisis_returns], axis=1).fillna(0.0)
    hedge_returns = v14._load_hedge_returns(close.index, str(cfg["hedge_asset"]))
    positions: dict[str, float] = {}
    overlay_weights: dict[str, float] = {"cash": 0.0}
    hedge_weight = 0.0
    returns: list[float] = []
    return_dates: list[pd.Timestamp] = []
    turnovers: list[float] = []
    hedge_turnovers: list[float] = []
    overlay_turnovers: list[float] = []
    active_counts: list[int] = []
    event_counts: list[int] = []
    alt_counts: list[int] = []
    crisis_counts: list[int] = []
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
                str(symbol) for symbol in latest.index if close.iloc[start_idx:t][symbol].count() >= 252
            ]
            if not symbols:
                new_positions: dict[str, float] = {}
                gross = 0.0
                bear = True
                alt_counts.append(0)
            else:
                close_window = close.iloc[max(start_idx, t - 252) : t + 1][symbols]
                gross, bear = v9._market_exposure(close_window, base_cfg)
                if bear and _bear_reentry_signal(close_window, cfg):
                    gross = max(gross, float(cfg.get("bear_reentry_exposure", gross)))
                    if int(cfg.get("bear_reentry_treat_as_normal", 1)):
                        bear = False
                bear_counts += int(bear)
                tech_weights = v9._dynamic_ic_weights(ic_history, date, base_cfg)
                tech_score = pd.Series(0.0, index=symbols)
                tech_total = pd.Series(0.0, index=symbols)
                for name, weight in tech_weights.items():
                    z = _safe_z(factors[name].iloc[local_t].reindex(symbols))
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
                defensive_score = v16._defensive_expert_score(factors, local_t, symbols, snapshot, cfg)
                alt_score, alt_coverage = _alt_data_score(alt_features, date, symbols, cfg)
                alt_counts.append(alt_coverage)
                combined = _combine_v17_experts(
                    tech_score,
                    fund_score,
                    event_score,
                    defensive_score,
                    alt_score,
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
            trend_overlay = v14._trend_overlay_weights(trend_prices, t, residual, bear, cfg)
            crisis_overlay = _crisis_overlay_weights(crisis_prices, crisis_roles, t, residual, bear, cfg)
            new_overlay = _merge_overlay_weights(trend_overlay, crisis_overlay)
            cost_today, turnover_today = v9._rebalance_cost(new_positions, positions, base_cfg)
            hedge_turnover = abs(new_hedge - hedge_weight)
            cost_today += hedge_turnover * float(cfg["hedge_cost"])
            crisis_asset_set = set(crisis_prices.columns)
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
            turnovers.append(turnover_today)
            hedge_turnovers.append(hedge_turnover)
            overlay_turnovers.append(overlay_turnover)
            total_cost += cost_today
            active_counts.append(len(positions))
            crisis_counts.append(
                sum(1 for asset, weight in new_overlay.items() if asset in crisis_asset_set and abs(weight) > 0)
            )
            exposure_values.append(
                sum(abs(weight) for weight in positions.values())
                + abs(hedge_weight)
                + sum(
                    abs(weight)
                    for asset, weight in overlay_weights.items()
                    if asset != "cash"
                )
            )
        day_return = -cost_today
        current = close.iloc[t]
        nxt = close.iloc[t + 1]
        for symbol, weight in positions.items():
            p0 = current.get(symbol, np.nan)
            p1 = nxt.get(symbol, np.nan)
            if pd.notna(p0) and pd.notna(p1) and p0 > 0 and p1 > 0:
                day_return += weight * (float(p1) / float(p0) - 1.0)
        overlay_day = overlay_returns.iloc[t + 1]
        for asset, weight in overlay_weights.items():
            day_return += weight * float(overlay_day.get(asset, 0.0))
        day_return += hedge_weight * float(hedge_returns.iloc[t + 1])
        returns.append(day_return)
        return_dates.append(pd.Timestamp(close.index[t + 1]))
        equity *= 1.0 + day_return
        peak_equity = max(peak_equity, equity)
    if len(returns) < 50:
        return None
    arr = np.array(returns, dtype=float)
    stats = _return_stats(arr, float(base_cfg["risk_free_rate"]))
    return_index = pd.DatetimeIndex(return_dates)
    recent_mask = return_index >= pd.Timestamp(RECENT_METRICS_START)
    recent_stats = _return_stats(arr[recent_mask], float(base_cfg["risk_free_rate"]))
    return {
        **stats,
        "avg_active_stocks": round(float(np.mean(active_counts)), 1) if active_counts else 0.0,
        "avg_event_symbols": round(float(np.mean(event_counts)), 1) if event_counts else 0.0,
        "avg_alt_symbols": round(float(np.mean(alt_counts)), 1) if alt_counts else 0.0,
        "max_alt_symbols": int(max(alt_counts)) if alt_counts else 0,
        "avg_crisis_assets": round(float(np.mean(crisis_counts)), 2) if crisis_counts else 0.0,
        "avg_turnover": round(float(np.mean(turnovers)), 4) if turnovers else 0.0,
        "avg_hedge_turnover": round(float(np.mean(hedge_turnovers)), 4) if hedge_turnovers else 0.0,
        "avg_overlay_turnover": round(float(np.mean(overlay_turnovers)), 4) if overlay_turnovers else 0.0,
        "avg_exposure": round(float(np.mean(exposure_values)), 4) if exposure_values else 0.0,
        "bear_rebalances": int(bear_counts),
        "total_cost": round(total_cost, 4),
        "recent_metrics_start": RECENT_METRICS_START,
        "recent": recent_stats,
    }


def _walk_forward(
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
        train = _hedged_backtest_v17(
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
            0,
            train_end,
            cfg,
        )
        context_start = max(0, test_start - int(cfg["warmup_days"]))
        oos = _hedged_backtest_v17(
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
            "avg_alt_symbols_oos": oos["avg_alt_symbols"] if oos else None,
            "avg_crisis_assets_oos": oos["avg_crisis_assets"] if oos else None,
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
        "C": bool(full and full.get("avg_alt_symbols", 0.0) >= float(CONFIG["min_alt_coverage"])),
    }
    result["A"] = all(result.values())
    return result


def _candidate_configs() -> list[tuple[str, dict[str, float | int | str]]]:
    variants: dict[str, dict[str, float | int | str]] = {
        "v17_v16_comparable": {
            "gross_exposure": 0.95,
            "target_vol": 0.14,
            "max_position_pct": 0.035,
            "normal_technical_weight": 0.45,
            "normal_fundamental_weight": 0.35,
            "normal_earnings_weight": 0.10,
            "normal_defensive_weight": 0.10,
            "normal_alt_weight": 0.0,
            "bear_technical_weight": 0.15,
            "bear_fundamental_weight": 0.35,
            "bear_earnings_weight": 0.20,
            "bear_defensive_weight": 0.30,
            "bear_alt_weight": 0.0,
            "crisis_sleeve_weight": 0.0,
            "bear_crisis_sleeve_weight": 0.0,
        },
        "v17_alt_light": {},
        "v18_revision_selected": {
            "gross_exposure": 0.95,
            "target_vol": 0.14,
            "max_position_pct": 0.035,
            "rebalance_freq": 20,
            "normal_technical_weight": 0.35,
            "normal_fundamental_weight": 0.30,
            "normal_earnings_weight": 0.10,
            "normal_defensive_weight": 0.10,
            "normal_alt_weight": 0.15,
            "bear_technical_weight": 0.10,
            "bear_fundamental_weight": 0.30,
            "bear_earnings_weight": 0.15,
            "bear_defensive_weight": 0.25,
            "bear_alt_weight": 0.20,
            "crisis_sleeve_weight": 0.0,
            "bear_crisis_sleeve_weight": 0.0,
            "alt_analyst_weight": 0.05,
            "alt_revision_weight": 0.75,
            "alt_northbound_weight": 0.05,
            "alt_flow_weight": 0.05,
            "alt_liquidity_weight": 0.10,
        },
        "v18_revision_selected_freq10": {
            "gross_exposure": 0.95,
            "target_vol": 0.14,
            "max_position_pct": 0.035,
            "normal_technical_weight": 0.35,
            "normal_fundamental_weight": 0.30,
            "normal_earnings_weight": 0.10,
            "normal_defensive_weight": 0.10,
            "normal_alt_weight": 0.15,
            "bear_technical_weight": 0.10,
            "bear_fundamental_weight": 0.30,
            "bear_earnings_weight": 0.15,
            "bear_defensive_weight": 0.25,
            "bear_alt_weight": 0.20,
            "crisis_sleeve_weight": 0.0,
            "bear_crisis_sleeve_weight": 0.0,
            "alt_analyst_weight": 0.05,
            "alt_revision_weight": 0.75,
            "alt_northbound_weight": 0.05,
            "alt_flow_weight": 0.05,
            "alt_liquidity_weight": 0.10,
        },
        "v18_revision_stable_freq10": {
            "gross_exposure": 0.95,
            "target_vol": 0.14,
            "max_position_pct": 0.035,
            "normal_technical_weight": 0.42,
            "normal_fundamental_weight": 0.33,
            "normal_earnings_weight": 0.10,
            "normal_defensive_weight": 0.10,
            "normal_alt_weight": 0.05,
            "bear_technical_weight": 0.15,
            "bear_fundamental_weight": 0.35,
            "bear_earnings_weight": 0.20,
            "bear_defensive_weight": 0.25,
            "bear_alt_weight": 0.05,
            "crisis_sleeve_weight": 0.0,
            "bear_crisis_sleeve_weight": 0.0,
            "alt_revision_mode": "stable_rating_60d",
            "alt_analyst_weight": 0.0,
            "alt_revision_weight": 1.0,
            "alt_northbound_weight": 0.0,
            "alt_flow_weight": 0.0,
            "alt_liquidity_weight": 0.0,
        },
        "v19_reentry_stable_freq10": {
            "gross_exposure": 0.95,
            "target_vol": 0.14,
            "max_position_pct": 0.035,
            "normal_technical_weight": 0.42,
            "normal_fundamental_weight": 0.33,
            "normal_earnings_weight": 0.10,
            "normal_defensive_weight": 0.10,
            "normal_alt_weight": 0.05,
            "bear_technical_weight": 0.15,
            "bear_fundamental_weight": 0.35,
            "bear_earnings_weight": 0.20,
            "bear_defensive_weight": 0.25,
            "bear_alt_weight": 0.05,
            "crisis_sleeve_weight": 0.0,
            "bear_crisis_sleeve_weight": 0.0,
            "alt_revision_mode": "stable_rating_60d",
            "alt_analyst_weight": 0.0,
            "alt_revision_weight": 1.0,
            "alt_northbound_weight": 0.0,
            "alt_flow_weight": 0.0,
            "alt_liquidity_weight": 0.0,
            "bear_reentry_enabled": 1,
            "bear_reentry_lookback": 20,
            "bear_reentry_momentum_threshold": 0.02,
            "bear_reentry_breadth_threshold": 0.55,
            "bear_reentry_exposure": 0.45,
            "bear_reentry_treat_as_normal": 1,
        },
        "v22_stable_cash_heavy": {
            "gross_exposure": 0.95,
            "target_vol": 0.14,
            "max_position_pct": 0.035,
            "normal_technical_weight": 0.42,
            "normal_fundamental_weight": 0.33,
            "normal_earnings_weight": 0.10,
            "normal_defensive_weight": 0.10,
            "normal_alt_weight": 0.05,
            "bear_technical_weight": 0.15,
            "bear_fundamental_weight": 0.35,
            "bear_earnings_weight": 0.20,
            "bear_defensive_weight": 0.25,
            "bear_alt_weight": 0.05,
            "trend_sleeve_weight": 0.15,
            "bear_trend_sleeve_weight": 0.25,
            "crisis_sleeve_weight": 0.0,
            "bear_crisis_sleeve_weight": 0.0,
            "alt_revision_mode": "stable_rating_60d",
            "alt_analyst_weight": 0.0,
            "alt_revision_weight": 1.0,
            "alt_northbound_weight": 0.0,
            "alt_flow_weight": 0.0,
            "alt_liquidity_weight": 0.0,
        },
        "v22_stable_low_hedge_cash": {
            "gross_exposure": 0.95,
            "target_vol": 0.14,
            "max_position_pct": 0.035,
            "normal_technical_weight": 0.42,
            "normal_fundamental_weight": 0.33,
            "normal_earnings_weight": 0.10,
            "normal_defensive_weight": 0.10,
            "normal_alt_weight": 0.05,
            "bear_technical_weight": 0.15,
            "bear_fundamental_weight": 0.35,
            "bear_earnings_weight": 0.20,
            "bear_defensive_weight": 0.25,
            "bear_alt_weight": 0.05,
            "normal_hedge_weight": 0.05,
            "bear_hedge_weight": 0.25,
            "trend_sleeve_weight": 0.15,
            "bear_trend_sleeve_weight": 0.25,
            "crisis_sleeve_weight": 0.0,
            "bear_crisis_sleeve_weight": 0.0,
            "alt_revision_mode": "stable_rating_60d",
            "alt_analyst_weight": 0.0,
            "alt_revision_weight": 1.0,
            "alt_northbound_weight": 0.0,
            "alt_flow_weight": 0.0,
            "alt_liquidity_weight": 0.0,
        },
        "v22_stable_no_trend_overlay": {
            "gross_exposure": 0.95,
            "target_vol": 0.14,
            "max_position_pct": 0.035,
            "normal_technical_weight": 0.42,
            "normal_fundamental_weight": 0.33,
            "normal_earnings_weight": 0.10,
            "normal_defensive_weight": 0.10,
            "normal_alt_weight": 0.05,
            "bear_technical_weight": 0.15,
            "bear_fundamental_weight": 0.35,
            "bear_earnings_weight": 0.20,
            "bear_defensive_weight": 0.25,
            "bear_alt_weight": 0.05,
            "trend_sleeve_weight": 0.0,
            "bear_trend_sleeve_weight": 0.0,
            "crisis_sleeve_weight": 0.0,
            "bear_crisis_sleeve_weight": 0.0,
            "alt_revision_mode": "stable_rating_60d",
            "alt_analyst_weight": 0.0,
            "alt_revision_weight": 1.0,
            "alt_northbound_weight": 0.0,
            "alt_flow_weight": 0.0,
            "alt_liquidity_weight": 0.0,
        },
        "v18_revision_light": {
            "gross_exposure": 0.95,
            "target_vol": 0.14,
            "max_position_pct": 0.035,
            "rebalance_freq": 20,
            "normal_technical_weight": 0.40,
            "normal_fundamental_weight": 0.35,
            "normal_earnings_weight": 0.10,
            "normal_defensive_weight": 0.10,
            "normal_alt_weight": 0.05,
            "bear_technical_weight": 0.15,
            "bear_fundamental_weight": 0.35,
            "bear_earnings_weight": 0.20,
            "bear_defensive_weight": 0.25,
            "bear_alt_weight": 0.05,
            "crisis_sleeve_weight": 0.0,
            "bear_crisis_sleeve_weight": 0.0,
            "alt_analyst_weight": 0.05,
            "alt_revision_weight": 0.75,
            "alt_northbound_weight": 0.05,
            "alt_flow_weight": 0.05,
            "alt_liquidity_weight": 0.10,
        },
        "v17_alt_heavy": {
            "normal_alt_weight": 0.20,
            "bear_alt_weight": 0.30,
            "normal_technical_weight": 0.35,
            "normal_fundamental_weight": 0.25,
            "bear_defensive_weight": 0.20,
        },
        "v17_crisis_light": {
            "normal_alt_weight": 0.05,
            "bear_alt_weight": 0.10,
            "crisis_sleeve_weight": 0.15,
            "bear_crisis_sleeve_weight": 0.35,
        },
        "v17_crisis_heavy": {
            "crisis_sleeve_weight": 0.25,
            "bear_crisis_sleeve_weight": 0.50,
            "bear_hedge_weight": 0.25,
        },
        "v17_v16_exposure": {
            "gross_exposure": 0.95,
            "target_vol": 0.14,
            "max_position_pct": 0.035,
            "normal_alt_weight": 0.10,
            "bear_alt_weight": 0.20,
            "crisis_sleeve_weight": 0.10,
            "bear_crisis_sleeve_weight": 0.25,
        },
    }
    result = []
    requested = {
        item.strip()
        for item in os.environ.get("QUANT_V17_CANDIDATES", "").split(",")
        if item.strip()
    }
    for label, updates in variants.items():
        if requested and label not in requested:
            continue
        cfg = deepcopy(CONFIG)
        cfg.update(updates)
        result.append((label, cfg))
    if requested and not result:
        raise ValueError(f"QUANT_V17_CANDIDATES matched no variants: {sorted(requested)}")
    return result


def _load_inputs() -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    dict[str, str],
    dict[str, pd.DataFrame],
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    dict[str, str],
]:
    close, volume, amount, turnover = base._load_aligned_data()
    industry_map = base._load_industries()
    fundamentals = _load_fundamentals()
    earnings_events = v15._load_earnings_events(close.columns)
    alt_features = _load_alt_features(close.columns)
    crisis_prices, crisis_roles = _load_crisis_prices(close.index)
    print(
        f"data symbols={close.shape[1]} dates={close.shape[0]} "
        f"{close.index.min().date()}->{close.index.max().date()} "
        f"industry={len(industry_map)} fundamentals={len(fundamentals)} "
        f"earnings_symbols={earnings_events['code'].nunique()} events={len(earnings_events)} "
        f"alt_rows={len(alt_features)} alt_symbols="
        f"{alt_features['code'].nunique() if not alt_features.empty else 0} "
        f"crisis_assets={crisis_prices.shape[1]} "
        f"stock_alt_file={STOCK_ALT_FEATURE_FILE} crisis_file={CRISIS_ALT_FEATURE_FILE}",
        flush=True,
    )
    return (
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
    )


def _production_blockers() -> list[str]:
    return [
        "V17 is research-only; sparse public snapshots are not a full PIT historical alpha set",
        "analyst consensus and fund-flow snapshots require daily archival or approved PIT provider replacement",
        "intraday minute data currently covers only a recent rolling window/sample archive",
        "futures/ETF crisis sleeve still requires broker/exchange-backed margin, rollover, account binding and position reconciliation evidence",
        "requires full WF/OOS, capacity, slippage, borrow/short and paper-trading evidence before production",
    ]


def _enforce_finalist_archive_gate(summary: dict[str, Any]) -> None:
    if not REQUIRE_ARCHIVE_COMPLETE_FOR_FINALIST:
        return
    if not summary or summary.get("summary_missing"):
        raise RuntimeError(
            "V17 finalist/WF is blocked: alternative-data feature summary is missing. "
            "Run scripts/build_alt_data_features_v17.py in archive mode first, or set "
            "QUANT_V17_REQUIRE_ARCHIVE_COMPLETE=0 only for an explicitly non-production replay."
        )
    if not bool(summary.get("archive_mode")):
        raise RuntimeError(
            "V17 finalist/WF is blocked: feature summary is not archive-bound "
            "(archive_mode=false)."
        )
    if not bool(summary.get("archive_complete_for_builder")):
        raise RuntimeError(
            "V17 finalist/WF is blocked: archive_complete_for_builder=false; "
            f"live_fallback_files={summary.get('live_fallback_files', [])}."
        )
    if not summary.get("archive_manifest_sha256"):
        raise RuntimeError("V17 finalist/WF is blocked: archive manifest hash is missing.")


def main() -> None:
    started = time.time()
    mode = os.environ.get("QUANT_V17_MODE", "grid").strip().lower()
    alt_feature_summary = _load_alt_feature_summary()
    if mode == "finalist":
        _enforce_finalist_archive_gate(alt_feature_summary)
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
    ) = _load_inputs()
    if mode == "grid":
        rows: list[dict[str, Any]] = []
        for label, cfg in _candidate_configs():
            full = _hedged_backtest_v17(
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
            "version": "V17-alt-data-crisis-alpha-grid",
            "research_only": True,
            "alt_feature_rows": len(alt_features),
            "alt_feature_symbols": int(alt_features["code"].nunique()) if not alt_features.empty else 0,
            "alt_feature_dates": int(alt_features["date"].nunique()) if not alt_features.empty else 0,
            "stock_alt_feature_file": STOCK_ALT_FEATURE_FILE,
            "crisis_alt_feature_file": CRISIS_ALT_FEATURE_FILE,
            "alt_feature_summary": alt_feature_summary,
            "crisis_assets": list(crisis_prices.columns),
            "production_blockers": _production_blockers(),
            "results": rows,
            "elapsed_s": round(time.time() - started, 1),
        }
        out_path = _output_path("quant_logic_research_v17_alt_data_grid.json")
    elif mode == "finalist":
        label = os.environ.get("QUANT_V17_FINALIST", "v17_alt_light").strip()
        cfg_map = {name: cfg for name, cfg in _candidate_configs()}
        if label not in cfg_map:
            raise ValueError(f"unknown finalist {label}; choose from {sorted(cfg_map)}")
        cfg = cfg_map[label]
        full = _hedged_backtest_v17(
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
            alt_features,
            crisis_prices,
            crisis_roles,
            cfg,
        )
        report = {
            "ts": datetime.now().isoformat(),
            "version": "V17-alt-data-crisis-alpha-finalist",
            "label": label,
            "research_only": True,
            "alt_feature_rows": len(alt_features),
            "alt_feature_symbols": int(alt_features["code"].nunique()) if not alt_features.empty else 0,
            "alt_feature_dates": int(alt_features["date"].nunique()) if not alt_features.empty else 0,
            "stock_alt_feature_file": STOCK_ALT_FEATURE_FILE,
            "crisis_alt_feature_file": CRISIS_ALT_FEATURE_FILE,
            "alt_feature_summary": alt_feature_summary,
            "production_blockers": _production_blockers(),
            "config": cfg,
            "full": full,
            "wf": wf,
            "gates": _gates(full, wf),
            "elapsed_s": round(time.time() - started, 1),
        }
        out_path = _output_path(f"quant_logic_research_v17_{label}_finalist.json")
        print(f"gates={report['gates']}", flush=True)
    else:
        raise ValueError("QUANT_V17_MODE must be grid or finalist")
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {out_path}", flush=True)


if __name__ == "__main__":
    main()
