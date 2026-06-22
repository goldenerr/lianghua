#!/usr/bin/env python3
"""Research V8: search portfolio overlays for the V7 fundamental sleeve.

This is research only. It intentionally does not change production strategy
configuration or readiness gates.
"""

from __future__ import annotations

import json
import time
from copy import deepcopy
from datetime import datetime
from itertools import product
from typing import Any

import validate_quant_logic_v5_9 as base
from _paths import RESULTS_DIR
from research_fundamental_quality_v7 import (
    CONFIG as V7_CONFIG,
)
from research_fundamental_quality_v7 import (
    FACTOR_WEIGHTS,
    _load_fundamentals,
    backtest,
    gates,
    walk_forward,
)

RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def _candidate_configs() -> list[dict[str, float | int]]:
    configs: list[dict[str, float | int]] = []
    grid = product(
        [40, 60],
        [40, 60],
        [0.08, 0.12],
        [0.10, 0.15],
        [0.25, 0.35],
        [0.03, 0.05],
    )
    for top_n, rebalance_freq, target_vol, dd_reduce, defensive, cash in grid:
        cfg = deepcopy(V7_CONFIG)
        cfg.update(
            {
                "top_n": top_n,
                "rebalance_freq": rebalance_freq,
                "target_vol": target_vol,
                "dd_reduce_threshold": dd_reduce,
                "defensive_exposure": defensive,
                "cash_exposure": cash,
                "max_position_pct": 0.025 if top_n == 40 else 0.02,
                "max_per_industry": 4,
            }
        )
        configs.append(cfg)
    return configs


def _score_full(metrics: dict[str, Any] | None) -> float:
    if not metrics or metrics["sharpe_ratio"] is None:
        return -999.0
    sharpe = float(metrics["sharpe_ratio"])
    mdd = abs(float(metrics["max_drawdown"]))
    ann = float(metrics["annual_return"])
    return sharpe + 0.5 * ann - max(0.0, mdd - 0.15) * 4.0


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

    screened: list[dict[str, Any]] = []
    configs = _candidate_configs()
    for i, cfg in enumerate(configs, start=1):
        full = backtest(close, fundamentals, industry_map, 0, len(close), cfg)
        row = {"rank_score": round(_score_full(full), 4), "full": full, "config": cfg}
        screened.append(row)
        print(
            f"[{i}/{len(configs)}] score={row['rank_score']} "
            f"sharpe={None if not full else full['sharpe_ratio']} "
            f"mdd={None if not full else full['max_drawdown']} "
            f"ann={None if not full else full['annual_return']}",
            flush=True,
        )

    top = sorted(screened, key=lambda row: row["rank_score"], reverse=True)[:3]
    finalists: list[dict[str, Any]] = []
    for i, row in enumerate(top, start=1):
        print(f"walk-forward finalist {i}/3 score={row['rank_score']}", flush=True)
        wf = walk_forward(close, fundamentals, industry_map, row["config"])
        row = dict(row)
        row["wf"] = wf
        row["gates"] = gates(row["full"], wf)
        finalists.append(row)

    report = {
        "ts": datetime.now().isoformat(),
        "version": "V8-portfolio-overlay-search",
        "research_only": True,
        "data": {
            "symbols": int(close.shape[1]),
            "dates": int(close.shape[0]),
            "start": str(close.index.min().date()),
            "end": str(close.index.max().date()),
            "industry_mapped": len(industry_map),
            "fundamental_mapped": len(fundamentals),
        },
        "factor_weights": FACTOR_WEIGHTS,
        "screened_count": len(screened),
        "top_full": top,
        "finalists": finalists,
        "elapsed_s": round(time.time() - started, 1),
    }
    out_path = RESULTS_DIR / "quant_logic_research_v8_portfolio_overlay.json"
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"Wrote {out_path}")
    for i, row in enumerate(finalists, start=1):
        print(f"finalist {i}: full={row['full']} wf={row['wf']} gates={row['gates']}")


if __name__ == "__main__":
    main()
