#!/usr/bin/env python3
"""Run a conservative aligned-data parameter search for the V5.9 research logic.

This is research validation only. It reuses the date-aligned, cost-aware
backtest from ``validate_quant_logic_v5_9.py`` and writes a report with the
best candidates plus walk-forward evidence for the top rows.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from itertools import product
from typing import Any, cast

import numpy as np
import validate_quant_logic_v5_9 as v
from _paths import RESULTS_DIR

RESULTS_DIR.mkdir(parents=True, exist_ok=True)

WEIGHT_SETS: dict[str, dict[str, float]] = {
    "v59_original": {
        "rsi": 0.25,
        "bollinger": 0.25,
        "momentum": 0.20,
        "macd": 0.15,
        "vol_dev": 0.10,
        "low_vol": 0.05,
    },
    "defensive_low_vol": {
        "rsi": 0.20,
        "bollinger": 0.20,
        "momentum": 0.15,
        "macd": 0.10,
        "vol_dev": 0.10,
        "low_vol": 0.25,
    },
    "mean_reversion_heavy": {
        "rsi": 0.32,
        "bollinger": 0.32,
        "momentum": 0.12,
        "macd": 0.08,
        "vol_dev": 0.08,
        "low_vol": 0.08,
    },
}


def _objective(full: dict[str, Any] | None) -> float:
    if not full or full.get("sharpe_ratio") is None:
        return -999.0
    sharpe = float(full["sharpe_ratio"])
    drawdown_penalty = max(0.0, abs(float(full["max_drawdown"])) - 0.15) * 4.0
    turnover_penalty = max(0.0, float(full["avg_turnover"]) - 1.2) * 0.2
    return sharpe - drawdown_penalty - turnover_penalty


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    numeric = float(value)
    return numeric if np.isfinite(numeric) else None


def main() -> None:
    started = time.time()
    print("Loading aligned data once for optimization...", flush=True)
    close, volume, amount, turnover = v._load_aligned_data()
    industry_map = v._load_industries()
    print(
        f"  symbols={close.shape[1]} dates={close.shape[0]} "
        f"{close.index.min().date()}->{close.index.max().date()} "
        f"industry={len(industry_map)}",
        flush=True,
    )

    grid = list(
        product(
            [25, 35, 50],
            [60, 90, 120],
            [0.0, 0.2, 0.3],
            WEIGHT_SETS.items(),
        )
    )
    rows: list[dict[str, Any]] = []
    original_weights = dict(v.V35_WEIGHTS)
    try:
        for idx, (top_n, freq, large_exclude_pct, weight_item) in enumerate(grid, start=1):
            weight_name, weights = weight_item
            cfg = dict(v.CONFIG)
            cfg["top_n"] = top_n
            cfg["rebalance_freq"] = freq
            cfg["large_cap_exclude_pct"] = large_exclude_pct
            v.V35_WEIGHTS.clear()
            v.V35_WEIGHTS.update(weights)
            full = v.backtest(
                close,
                volume,
                amount,
                turnover,
                industry_map,
                0,
                len(close),
                cfg,
                exclude_large_caps=large_exclude_pct > 0,
            )
            row = {
                "rank_input": idx,
                "top_n": top_n,
                "rebalance_freq": freq,
                "large_cap_exclude_pct": large_exclude_pct,
                "weights": weight_name,
                "objective": round(_objective(full), 4),
                "sharpe_ratio": _safe_float(full.get("sharpe_ratio") if full else None),
                "max_drawdown": _safe_float(full.get("max_drawdown") if full else None),
                "win_rate": _safe_float(full.get("win_rate") if full else None),
                "annual_return": _safe_float(full.get("annual_return") if full else None),
                "avg_turnover": _safe_float(full.get("avg_turnover") if full else None),
                "avg_active_stocks": _safe_float(full.get("avg_active_stocks") if full else None),
                "full": full,
            }
            rows.append(row)
            print(
                f"[{idx}/{len(grid)}] n={top_n} f={freq} x={large_exclude_pct:.1f} "
                f"w={weight_name} sharpe={row['sharpe_ratio']} "
                f"mdd={row['max_drawdown']} obj={row['objective']}",
                flush=True,
            )
    finally:
        v.V35_WEIGHTS.clear()
        v.V35_WEIGHTS.update(original_weights)

    rows.sort(key=lambda item: float(item["objective"]), reverse=True)
    wf_rows: list[dict[str, Any]] = []
    for rank, row in enumerate(rows[:5], start=1):
        top_n = int(cast(int, row["top_n"]))
        rebalance_freq = int(cast(int, row["rebalance_freq"]))
        large_cap_exclude_pct = float(cast(float, row["large_cap_exclude_pct"]))
        full = cast(dict[str, Any] | None, row["full"])
        cfg = dict(v.CONFIG)
        cfg["top_n"] = top_n
        cfg["rebalance_freq"] = rebalance_freq
        cfg["large_cap_exclude_pct"] = large_cap_exclude_pct
        weights = WEIGHT_SETS[str(row["weights"])]
        v.V35_WEIGHTS.clear()
        v.V35_WEIGHTS.update(weights)
        print(f"Walk-forward top {rank}: {row['weights']} {cfg}", flush=True)
        wf = v.walk_forward(
            close,
            volume,
            amount,
            turnover,
            industry_map,
            cfg,
            exclude_large_caps=large_cap_exclude_pct > 0,
        )
        gates = v._gates(full, wf)
        gates["A"] = all(gates.values())
        wf_rows.append({"rank": rank, "candidate": row, "wf": wf, "gates": gates})
    v.V35_WEIGHTS.clear()
    v.V35_WEIGHTS.update(original_weights)

    report = {
        "ts": datetime.now().isoformat(),
        "version": "V5.9-aligned-parameter-optimization",
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
        "grid_size": len(grid),
        "top_full_backtests": rows[:10],
        "top_walk_forward": wf_rows,
        "elapsed_s": round(time.time() - started, 1),
    }
    out_path = RESULTS_DIR / "quant_logic_optimization_v5_9_aligned.json"
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"Wrote {out_path}", flush=True)


if __name__ == "__main__":
    main()
