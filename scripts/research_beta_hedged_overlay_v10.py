#!/usr/bin/env python3
"""Research V10: V9 dynamic IC sleeve plus index-futures proxy hedge.

The hedge uses benchmark index returns as a futures proxy. This is research only:
production would require exchange-backed futures execution, margin accounting,
rollover approval, and position reconciliation evidence.
"""

from __future__ import annotations

import json
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
    }
)


def _load_hedge_returns(index: pd.DatetimeIndex, asset_id: str) -> pd.Series:
    path = PROJECT_DIR / "data" / "benchmarks" / f"{asset_id}.parquet"
    if not path.exists():
        raise RuntimeError(f"missing hedge benchmark data: {path}")
    df = pd.read_parquet(path)
    return df["close"].astype(float).pct_change(fill_method=None).reindex(index).fillna(0.0)


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
    overlay_returns = v9._load_overlay_returns(close.index)
    hedge_returns = _load_hedge_returns(close.index, str(cfg["hedge_asset"]))
    positions: dict[str, float] = {}
    overlay_weights = {"cash": 0.0, "gold": 0.0}
    hedge_weight = 0.0
    returns: list[float] = []
    turnovers: list[float] = []
    hedge_turnovers: list[float] = []
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
                selected = v9._select(combined, industry_map, base_cfg)
                weight = min(gross / len(selected), float(base_cfg["max_position_pct"])) if selected else 0.0
                new_positions = {symbol: weight for symbol in selected}
            residual = max(0.0, 1.0 - sum(new_positions.values()))
            new_hedge = -(
                float(cfg["bear_hedge_weight"]) if bear else float(cfg["normal_hedge_weight"])
            ) * sum(new_positions.values())
            gold_weight = residual * (
                float(base_cfg["bear_gold_weight"]) if bear else float(base_cfg["normal_gold_weight"])
            )
            new_overlay = {"gold": gold_weight, "cash": residual - gold_weight}
            cost_today, turnover_today = v9._rebalance_cost(new_positions, positions, base_cfg)
            hedge_turnover = abs(new_hedge - hedge_weight)
            cost_today += hedge_turnover * float(cfg["hedge_cost"])
            positions = new_positions
            overlay_weights = new_overlay
            hedge_weight = new_hedge
            turnovers.append(turnover_today)
            hedge_turnovers.append(hedge_turnover)
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
        overlay_day = overlay_returns.iloc[t + 1]
        day_return += overlay_weights.get("cash", 0.0) * float(overlay_day.get("cash", 0.0))
        day_return += overlay_weights.get("gold", 0.0) * float(overlay_day.get("gold", 0.0))
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
        "version": "V10-beta-hedged-overlay",
        "research_only": True,
        "production_blockers": [
            "requires exchange-backed futures provider",
            "requires futures margin and rollover approval evidence",
            "requires position reconciliation for hedge leg",
        ],
        "config": CONFIG,
        "full": full,
        "wf": wf,
        "gates": _gates(full, wf),
        "elapsed_s": round(time.time() - started, 1),
    }
    out_path = RESULTS_DIR / "quant_logic_research_v10_beta_hedged_overlay.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {out_path}")
    print(f"gates={report['gates']}")


if __name__ == "__main__":
    main()
