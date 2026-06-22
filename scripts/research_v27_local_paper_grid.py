#!/usr/bin/env python3
"""Small V27 local paper-trading parameter stress test.

This is intentionally narrow and research-only. It helps identify whether the
current V5.9 factor stack is merely mis-parameterized on the V27 universe, or
whether the alpha design itself is failing recent out-of-sample conditions.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from _paths import PROJECT_DIR, RESULTS_DIR
from generate_local_preprod_evidence_v27 import (
    DEFAULT_UNIVERSE,
    _build_snapshot,
    _load_price_frames,
    _read_codes,
    _sha256_file,
)
from quant_trading.paper_trading import V59_CONFIG, PaperTradingEngine

DEFAULT_OUTPUT_JSON = RESULTS_DIR / "quant_v27_local_paper_param_grid.json"
DEFAULT_OUTPUT_CSV = RESULTS_DIR / "quant_v27_local_paper_param_grid.csv"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    parser.add_argument("--paper-days", type=int, default=90)
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    return parser.parse_args()


def _select_dates(
    frames: dict[str, pd.DataFrame],
    codes: list[str],
    paper_days: int,
) -> tuple[list[pd.Timestamp], dict[str, dict[pd.Timestamp, int]], dict[str, Any]]:
    idx_by_date = {
        code: {pd.Timestamp(index): idx for idx, index in enumerate(frame.index)}
        for code, frame in frames.items()
    }
    all_dates = sorted(pd.Timestamp(item) for item in set().union(*(set(mapping) for mapping in idx_by_date.values())))
    requested_min_symbol_coverage = min(len(frames), max(50, int(len(codes) * 0.80)))
    eligible_dates = []
    for current_date in all_dates:
        warm_count = sum(
            1
            for mapping in idx_by_date.values()
            if (idx := mapping.get(current_date)) is not None and idx >= 252
        )
        if warm_count >= requested_min_symbol_coverage:
            eligible_dates.append(current_date)
    if len(eligible_dates) < paper_days:
        raise RuntimeError(
            f"not enough eligible dates: have {len(eligible_dates)}, need {paper_days}, "
            f"min_symbol_coverage={requested_min_symbol_coverage}"
        )
    selected_dates = eligible_dates[-paper_days:]
    return selected_dates, idx_by_date, {
        "eligible_dates": len(eligible_dates),
        "requested_min_symbol_coverage": requested_min_symbol_coverage,
    }


def _config_grid() -> list[dict[str, Any]]:
    configs: list[dict[str, Any]] = []
    sector_caps = {20: 3, 35: 5, 50: 8, 80: 10}
    for rebalance_freq in (5, 10, 20):
        for top_n in (20, 35, 50, 80):
            configs.append(
                {
                    "name": f"freq{rebalance_freq}_top{top_n}_sector{sector_caps[top_n]}",
                    "rebalance_freq": rebalance_freq,
                    "top_n": top_n,
                    "max_per_sector": sector_caps[top_n],
                    "max_position_pct": V59_CONFIG["max_position_pct"],
                }
            )
    configs.extend(
        [
            {
                "name": "freq10_top35_sector5_half_gross",
                "rebalance_freq": 10,
                "top_n": 35,
                "max_per_sector": 5,
                "max_position_pct": 0.015,
            },
            {
                "name": "freq20_top35_sector5_half_gross",
                "rebalance_freq": 20,
                "top_n": 35,
                "max_per_sector": 5,
                "max_position_pct": 0.015,
            },
            {
                "name": "freq10_top50_sector8_defensive_mdd",
                "rebalance_freq": 10,
                "top_n": 50,
                "max_per_sector": 8,
                "max_position_pct": 0.02,
                "mdd_reduce_threshold": 0.05,
                "mdd_reduce_scale": 0.25,
                "mdd_stop_threshold": 0.10,
            },
        ]
    )
    return configs


def _run_config(
    cfg_update: dict[str, Any],
    frames: dict[str, pd.DataFrame],
    selected_dates: list[pd.Timestamp],
    idx_by_date: dict[str, dict[pd.Timestamp, int]],
) -> dict[str, Any]:
    engine = PaperTradingEngine(
        initial_capital=1_000_000,
        config={**V59_CONFIG, **{key: value for key, value in cfg_update.items() if key != "name"}},
        industry_data_path=PROJECT_DIR / "data" / "industry_fixed.parquet",
    )
    started = time.perf_counter()
    rebalance_days = 0
    price_coverage: list[int] = []
    active_positions: list[int] = []
    for day_idx, current_date in enumerate(selected_dates):
        tradable = {
            code: frame
            for code, frame in frames.items()
            if current_date in idx_by_date[code]
        }
        idx_by_symbol = {code: idx_by_date[code][current_date] for code in tradable}
        snapshot, prices = _build_snapshot(tradable, idx_by_symbol, current_date)
        price_coverage.append(len(prices))
        if day_idx == 0 or day_idx % int(engine.config["rebalance_freq"]) == 0:
            targets = engine.compute_positions(snapshot, current_date)
            engine.execute_rebalance(targets, prices, current_date.date().isoformat())
            rebalance_days += 1
        engine.account.update_market_values(prices)
        engine.days_traded += 1
        active_positions.append(len(engine.account.positions))
        if engine.stopped:
            break
    report = engine.generate_report()
    report.update(
        {
            "name": cfg_update["name"],
            "config": cfg_update,
            "completed_days": engine.days_traded,
            "rebalance_days": rebalance_days,
            "avg_price_coverage": round(float(np.mean(price_coverage)), 2) if price_coverage else 0.0,
            "min_price_coverage": int(min(price_coverage)) if price_coverage else 0,
            "avg_active_positions": round(float(np.mean(active_positions)), 2) if active_positions else 0.0,
            "max_active_positions": int(max(active_positions)) if active_positions else 0,
            "elapsed_s": round(time.perf_counter() - started, 3),
        }
    )
    report["passes_production_minimum_probe"] = bool(
        report.get("sharpe_ratio", -999) >= 1.2
        and report.get("max_drawdown", -999) >= -0.15
        and report.get("total_return", -999) > 0
    )
    report["research_warning"] = (
        "90-day local grid is parameter-probe evidence only; it is not WF/Purged-CV production evidence"
    )
    return report


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "name",
        "sharpe_ratio",
        "annual_return",
        "annual_volatility",
        "max_drawdown",
        "total_return",
        "total_trades",
        "avg_active_positions",
        "max_active_positions",
        "passes_production_minimum_probe",
        "elapsed_s",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})


def main() -> None:
    args = _parse_args()
    started = time.perf_counter()
    universe_path = Path(args.universe)
    codes = _read_codes(universe_path)
    frames = _load_price_frames(codes)
    selected_dates, idx_by_date, date_info = _select_dates(frames, codes, args.paper_days)
    rows = [
        _run_config(config, frames, selected_dates, idx_by_date)
        for config in _config_grid()
    ]
    rows.sort(
        key=lambda item: (
            float(item.get("sharpe_ratio", -999)),
            float(item.get("total_return", -999)),
        ),
        reverse=True,
    )
    output_json = Path(args.output_json)
    output_csv = Path(args.output_csv)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V27-local-paper-param-grid",
        "research_only": True,
        "production_ready": False,
        "universe": {
            "path": str(universe_path),
            "size": len(codes),
            "sha256": _sha256_file(universe_path),
        },
        "paper_window_start": selected_dates[0].date().isoformat(),
        "paper_window_end": selected_dates[-1].date().isoformat(),
        "paper_days": args.paper_days,
        "price_frame_symbols": len(frames),
        **date_info,
        "candidate_count": len(rows),
        "best": rows[0] if rows else None,
        "passes_production_minimum_probe_count": sum(
            1 for row in rows if row.get("passes_production_minimum_probe")
        ),
        "results": rows,
        "production_blockers": [
            "parameter probe is tuned on one recent 90-day window",
            "must rerun full WF/Purged-CV before production promotion",
            "external WORM/provider/approval/broker evidence remains required",
        ],
        "elapsed_s": round(time.perf_counter() - started, 3),
    }
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(output_csv, rows)
    best = payload["best"] or {}
    print(f"Wrote {output_json}")
    print(f"Wrote {output_csv}")
    print(
        "Best local grid: "
        f"{best.get('name')} sharpe={best.get('sharpe_ratio')} "
        f"mdd={best.get('max_drawdown')} "
        f"return={best.get('total_return')} "
        f"probe_passes={best.get('passes_production_minimum_probe')}"
    )


if __name__ == "__main__":
    main()
