#!/usr/bin/env python3
"""Score V23 alternative-alpha readiness from archived evidence.

The script does not promote any strategy. It turns the V21/V22 research
artifacts into a fail-closed readiness report so weak, sparse, or rejected
alpha sleeves cannot be mistaken for production evidence.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from _paths import ALT_FEATURES_DIR, RESULTS_DIR

PRODUCTION_SHARPE_GATE = 1.20
PRODUCTION_MDD_GATE = -0.15
PRODUCTION_WIN_RATE_GATE = 0.40
MIN_PRODUCTION_QUALIFIED_SIGNALS = 5
MIN_RESEARCH_DATES = 60
MIN_PRODUCTION_DATES = 500
MIN_PRODUCTION_COVERAGE = 500.0

DEFAULT_FEATURE_SUMMARY = "v21_archive_20260608_alt_feature_summary.json"
DEFAULT_IC_JSON = "quant_alt_factor_ic_v18_v21_archive_20260608.json"
DEFAULT_IC_CSV = "quant_alt_factor_ic_v18_v21_archive_20260608.csv"
DEFAULT_RISK_GRID = "quant_logic_research_v17_alt_data_grid_v22_risk_budget_grid.json"
DEFAULT_CRISIS_GRID = "quant_logic_research_v17_alt_data_grid_v22_crisis_grid.json"
DEFAULT_OUTPUT_JSON = "quant_alpha_readiness_v23.json"
DEFAULT_OUTPUT_CSV = "quant_alpha_readiness_v23.csv"

CATEGORY_TO_SECTION = {
    "analyst_consensus": "analyst_snapshot",
    "analyst_revision": "analyst_revision",
    "analyst_revision_rolling": "analyst_revision",
    "northbound": "northbound",
    "fund_flow": "fund_flow_rank",
    "order_flow": "big_deal",
    "margin_short": "margin",
    "intraday_microstructure": "intraday",
}

CATEGORY_LABELS = {
    "analyst_consensus": "Analyst consensus snapshot",
    "analyst_revision": "Dated analyst rating/revision events",
    "analyst_revision_rolling": "Rolling analyst revision signal",
    "northbound": "Northbound holding/flow",
    "fund_flow": "Main/fund-flow rank",
    "order_flow": "Big-deal/order-flow snapshot",
    "margin_short": "Margin/short availability proxy",
    "intraday_microstructure": "Intraday liquidity/reversal",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--feature-summary",
        default=os.getenv("QUANT_V23_FEATURE_SUMMARY", DEFAULT_FEATURE_SUMMARY),
        help="Feature summary JSON path or filename under data/alt_features.",
    )
    parser.add_argument(
        "--ic-json",
        default=os.getenv("QUANT_V23_IC_JSON", DEFAULT_IC_JSON),
        help="IC diagnostic JSON path or filename under data/backtest_results.",
    )
    parser.add_argument(
        "--ic-csv",
        default=os.getenv("QUANT_V23_IC_CSV", DEFAULT_IC_CSV),
        help="IC diagnostic CSV path or filename under data/backtest_results.",
    )
    parser.add_argument(
        "--risk-grid",
        default=os.getenv("QUANT_V23_RISK_GRID", DEFAULT_RISK_GRID),
        help="V22 risk-budget grid JSON path or filename under data/backtest_results.",
    )
    parser.add_argument(
        "--crisis-grid",
        default=os.getenv("QUANT_V23_CRISIS_GRID", DEFAULT_CRISIS_GRID),
        help="V22 crisis-sleeve grid JSON path or filename under data/backtest_results.",
    )
    parser.add_argument(
        "--output-json",
        default=os.getenv("QUANT_V23_OUTPUT_JSON", DEFAULT_OUTPUT_JSON),
        help="Output JSON path or filename under data/backtest_results.",
    )
    parser.add_argument(
        "--output-csv",
        default=os.getenv("QUANT_V23_OUTPUT_CSV", DEFAULT_OUTPUT_CSV),
        help="Output CSV path or filename under data/backtest_results.",
    )
    parser.add_argument(
        "--require-production-ready",
        action="store_true",
        help="Exit non-zero when the current evidence does not pass production gates.",
    )
    return parser.parse_args()


def _resolve(path_value: str, default_dir: Path) -> Path:
    path = Path(path_value)
    if path.is_absolute() or path.parent != Path("."):
        return path
    return default_dir / path


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"required evidence file is missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _finite_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(result):
        return None
    return result


def _bool_series(values: pd.Series) -> pd.Series:
    if values.dtype == bool:
        return values.fillna(False)
    return values.astype(str).str.lower().isin({"true", "1", "yes"})


def _score_component(value: float | None, denominator: float, cap: float) -> float:
    if value is None or denominator <= 0:
        return 0.0
    return max(0.0, min(value / denominator, 1.0)) * cap


def _best_ic_row(frame: pd.DataFrame) -> dict[str, Any]:
    valid = frame.dropna(subset=["rank_ic_mean"]).copy()
    if valid.empty:
        return {}
    valid["abs_ic"] = valid["rank_ic_mean"].abs()
    qualified = valid[valid["qualified_for_research_bool"]]
    source = qualified if not qualified.empty else valid
    row = source.sort_values(["abs_ic", "n_dates"], ascending=[False, False]).iloc[0]
    return {
        "factor": row["factor"],
        "horizon": int(row["horizon"]),
        "rank_ic_mean": _finite_float(row["rank_ic_mean"]),
        "rank_ic_t": _finite_float(row["rank_ic_t"]),
        "n_dates": int(row["n_dates"]),
        "avg_coverage": _finite_float(row["avg_coverage"]),
        "qualified_for_research": bool(row["qualified_for_research_bool"]),
    }


def _readiness_class(
    *,
    section_rows: int,
    max_dates: int,
    max_coverage: float,
    qualified_count: int,
    best_t_stat: float | None,
) -> str:
    if section_rows <= 0:
        return "missing_source_data"
    if max_dates < MIN_RESEARCH_DATES:
        return "data_gap_insufficient_pit_history"
    if max_coverage < 80:
        return "data_gap_thin_cross_section"
    if qualified_count <= 0:
        if best_t_stat is not None and abs(best_t_stat) >= 2.0:
            return "rejected_unstable_or_leaky_signal"
        return "weak_or_unqualified_signal"
    if qualified_count < MIN_PRODUCTION_QUALIFIED_SIGNALS:
        return "research_component_not_production"
    if max_dates < MIN_PRODUCTION_DATES or max_coverage < MIN_PRODUCTION_COVERAGE:
        return "research_component_needs_deeper_history"
    return "candidate_alpha_component"


def _score_category(
    category: str,
    frame: pd.DataFrame,
    feature_summary: dict[str, Any],
    portfolio_contribution_score: float,
) -> dict[str, Any]:
    section = CATEGORY_TO_SECTION.get(category)
    stock_sections = feature_summary.get("stock_sections") or {}
    section_rows = int(stock_sections.get(section, 0)) if section else 0
    complete_archive = bool(feature_summary.get("archive_complete_for_builder"))
    has_manifest = bool(feature_summary.get("archive_manifest_sha256"))
    no_live_fallback = not bool(feature_summary.get("live_fallback_files") or [])

    qualified = int(frame["qualified_for_research_bool"].sum())
    max_dates = int(pd.to_numeric(frame["n_dates"], errors="coerce").fillna(0).max())
    max_coverage = float(pd.to_numeric(frame["avg_coverage"], errors="coerce").fillna(0).max())
    factors = int(frame["factor"].nunique())
    best = _best_ic_row(frame)
    best_t = _finite_float(best.get("rank_ic_t")) if best else None
    best_ic = _finite_float(best.get("rank_ic_mean")) if best else None

    archive_score = 15.0 if complete_archive and has_manifest and no_live_fallback else 0.0
    history_score = _score_component(float(max_dates), float(MIN_PRODUCTION_DATES), 20.0)
    coverage_score = _score_component(max_coverage, MIN_PRODUCTION_COVERAGE, 15.0)
    signal_score = 0.0
    if qualified > 0:
        signal_score += min(qualified / MIN_PRODUCTION_QUALIFIED_SIGNALS, 1.0) * 12.0
        signal_score += _score_component(abs(best_t or 0.0), 5.0, 10.0)
        signal_score += _score_component(abs(best_ic or 0.0), 0.03, 8.0)
    total_score = archive_score + history_score + coverage_score + signal_score
    total_score += max(0.0, min(portfolio_contribution_score, 20.0))

    readiness = _readiness_class(
        section_rows=section_rows,
        max_dates=max_dates,
        max_coverage=max_coverage,
        qualified_count=qualified,
        best_t_stat=best_t,
    )
    if readiness == "candidate_alpha_component" and portfolio_contribution_score < 15.0:
        readiness = "research_component_not_production"

    return {
        "category": category,
        "label": CATEGORY_LABELS.get(category, category),
        "source_section": section,
        "source_rows": section_rows,
        "factors_tested": factors,
        "ic_rows": int(len(frame)),
        "max_n_dates": max_dates,
        "max_avg_coverage": round(max_coverage, 2),
        "qualified_count": qualified,
        "best_factor": best.get("factor"),
        "best_horizon": best.get("horizon"),
        "best_rank_ic_mean": best_ic,
        "best_rank_ic_t": best_t,
        "best_qualified": bool(best.get("qualified_for_research", False)),
        "archive_score": round(archive_score, 2),
        "history_score": round(history_score, 2),
        "coverage_score": round(coverage_score, 2),
        "signal_score": round(signal_score, 2),
        "portfolio_contribution_score": round(portfolio_contribution_score, 2),
        "readiness_score": round(total_score, 2),
        "readiness_class": readiness,
    }


def _portfolio_rows(report: dict[str, Any], experiment: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    raw_results = report.get("results") or []
    baseline = next(
        (item for item in raw_results if item.get("label") == "v18_revision_stable_freq10"),
        raw_results[0] if raw_results else {},
    )
    baseline_full = baseline.get("full") or {}
    baseline_sharpe = _finite_float(baseline_full.get("sharpe_ratio")) or -999.0
    baseline_recent = _finite_float((baseline_full.get("recent") or {}).get("sharpe_ratio"))

    for item in raw_results:
        full = item.get("full") or {}
        recent = full.get("recent") or {}
        sharpe = _finite_float(full.get("sharpe_ratio"))
        recent_sharpe = _finite_float(recent.get("sharpe_ratio"))
        max_drawdown = _finite_float(full.get("max_drawdown"))
        win_rate = _finite_float(full.get("win_rate"))
        status = "rejected"
        label = str(item.get("label"))
        if label == "v18_revision_stable_freq10":
            status = "current_best_research_baseline"
        elif (
            sharpe is not None
            and recent_sharpe is not None
            and sharpe > baseline_sharpe
            and (baseline_recent is None or recent_sharpe >= baseline_recent)
        ):
            status = "improved_research_candidate"
        rows.append(
            {
                "experiment": experiment,
                "label": label,
                "status": status,
                "sharpe_ratio": sharpe,
                "annual_return": _finite_float(full.get("annual_return")),
                "annual_volatility": _finite_float(full.get("annual_volatility")),
                "max_drawdown": max_drawdown,
                "win_rate": win_rate,
                "recent_sharpe_ratio": recent_sharpe,
                "avg_crisis_assets": _finite_float(full.get("avg_crisis_assets")),
                "avg_alt_symbols": _finite_float(full.get("avg_alt_symbols")),
                "total_cost": _finite_float(full.get("total_cost")),
                "passes_sharpe_gate": bool(
                    sharpe is not None and sharpe >= PRODUCTION_SHARPE_GATE
                ),
                "passes_mdd_gate": bool(
                    max_drawdown is not None and max_drawdown >= PRODUCTION_MDD_GATE
                ),
                "passes_win_gate": bool(
                    win_rate is not None and win_rate >= PRODUCTION_WIN_RATE_GATE
                ),
            }
        )
    return rows


def _portfolio_summary(
    risk_grid: dict[str, Any],
    crisis_grid: dict[str, Any],
) -> dict[str, Any]:
    rows = _portfolio_rows(risk_grid, "risk_budget") + _portfolio_rows(
        crisis_grid,
        "crisis_sleeve",
    )
    if not rows:
        return {
            "rows": [],
            "best": {},
            "risk_budget_status": "missing",
            "crisis_sleeve_status": "missing",
        }

    best = sorted(
        rows,
        key=lambda row: (
            row["sharpe_ratio"] if row["sharpe_ratio"] is not None else -999.0,
            row["max_drawdown"] if row["max_drawdown"] is not None else -999.0,
        ),
        reverse=True,
    )[0]
    risk_variants = [row for row in rows if row["experiment"] == "risk_budget"]
    crisis_variants = [row for row in rows if row["experiment"] == "crisis_sleeve"]
    return {
        "rows": rows,
        "best": best,
        "risk_budget_status": _experiment_status(risk_variants),
        "crisis_sleeve_status": _experiment_status(crisis_variants),
    }


def _experiment_status(rows: list[dict[str, Any]]) -> str:
    improved = [row for row in rows if row["status"] == "improved_research_candidate"]
    if improved:
        return "has_improved_research_candidate"
    if rows:
        return "rejected_no_incremental_portfolio_value"
    return "missing"


def _portfolio_contribution_scores(summary: dict[str, Any]) -> dict[str, float]:
    best = summary.get("best") or {}
    best_label = best.get("label")
    best_sharpe = _finite_float(best.get("sharpe_ratio"))
    scores = {category: 0.0 for category in CATEGORY_TO_SECTION}
    if best_label == "v18_revision_stable_freq10" and best_sharpe is not None:
        # The current baseline explicitly uses the stable rolling analyst rating
        # factor, but it still fails the production Sharpe gate.
        scores["analyst_revision_rolling"] = 10.0 if best_sharpe < PRODUCTION_SHARPE_GATE else 20.0
    return scores


def _production_blockers(
    *,
    feature_summary: dict[str, Any],
    ic_report: dict[str, Any],
    category_rows: list[dict[str, Any]],
    portfolio: dict[str, Any],
) -> list[str]:
    blockers: list[str] = []
    best = portfolio.get("best") or {}
    best_sharpe = _finite_float(best.get("sharpe_ratio"))
    if best_sharpe is None or best_sharpe < PRODUCTION_SHARPE_GATE:
        blockers.append(
            f"best local Sharpe {best_sharpe} is below production gate "
            f"{PRODUCTION_SHARPE_GATE}"
        )
    best_mdd = _finite_float(best.get("max_drawdown"))
    if best_mdd is None or best_mdd < PRODUCTION_MDD_GATE:
        blockers.append(
            f"best local max_drawdown {best_mdd} breaches gate {PRODUCTION_MDD_GATE}"
        )
    qualified_count = int(ic_report.get("qualified_count", 0))
    if qualified_count < MIN_PRODUCTION_QUALIFIED_SIGNALS:
        blockers.append(
            f"only {qualified_count} qualified alpha signal(s); "
            f"production threshold is {MIN_PRODUCTION_QUALIFIED_SIGNALS}"
        )
    weak_categories = [
        row["category"]
        for row in category_rows
        if str(row["readiness_class"]).startswith("data_gap")
    ]
    if weak_categories:
        blockers.append(
            "insufficient PIT history/cross-section for categories: "
            + ", ".join(sorted(weak_categories))
        )
    if feature_summary.get("archive_complete_for_builder") and feature_summary.get(
        "archive_manifest_sha256"
    ):
        blockers.append(
            "alternative-data archive is local research evidence; external "
            "WORM/CI artifact attestation is still required"
        )
    else:
        blockers.append("alternative-data archive completeness evidence is missing")
    if portfolio.get("risk_budget_status") != "has_improved_research_candidate":
        blockers.append("V22 risk-budget variants were rejected versus baseline")
    if portfolio.get("crisis_sleeve_status") != "has_improved_research_candidate":
        blockers.append("V22 crisis-alpha sleeve variants were rejected versus baseline")
    blockers.append("3-month paper-trading and small-live acceptance evidence is absent")
    return blockers


def _production_ready(portfolio: dict[str, Any], blockers: list[str]) -> bool:
    best = portfolio.get("best") or {}
    return (
        not blockers
        and bool(best.get("passes_sharpe_gate"))
        and bool(best.get("passes_mdd_gate"))
        and bool(best.get("passes_win_gate"))
    )


def main() -> None:
    args = _parse_args()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    feature_summary_path = _resolve(str(args.feature_summary), ALT_FEATURES_DIR)
    ic_json_path = _resolve(str(args.ic_json), RESULTS_DIR)
    ic_csv_path = _resolve(str(args.ic_csv), RESULTS_DIR)
    risk_grid_path = _resolve(str(args.risk_grid), RESULTS_DIR)
    crisis_grid_path = _resolve(str(args.crisis_grid), RESULTS_DIR)
    output_json_path = _resolve(str(args.output_json), RESULTS_DIR)
    output_csv_path = _resolve(str(args.output_csv), RESULTS_DIR)

    feature_summary = _read_json(feature_summary_path)
    ic_report = _read_json(ic_json_path)
    risk_grid = _read_json(risk_grid_path)
    crisis_grid = _read_json(crisis_grid_path)
    if not ic_csv_path.exists():
        raise FileNotFoundError(f"required IC CSV is missing: {ic_csv_path}")

    ic_frame = pd.read_csv(ic_csv_path)
    required_columns = {"factor", "category", "horizon", "n_dates", "avg_coverage"}
    missing = sorted(required_columns.difference(ic_frame.columns))
    if missing:
        raise RuntimeError(f"IC CSV missing required columns: {missing}")
    ic_frame["qualified_for_research_bool"] = _bool_series(
        ic_frame.get("qualified_for_research", pd.Series(False, index=ic_frame.index))
    )
    for column in ("horizon", "n_dates", "avg_coverage", "rank_ic_mean", "rank_ic_t"):
        if column in ic_frame.columns:
            ic_frame[column] = pd.to_numeric(ic_frame[column], errors="coerce")

    portfolio = _portfolio_summary(risk_grid, crisis_grid)
    contribution_scores = _portfolio_contribution_scores(portfolio)
    category_rows = []
    for category in sorted(set(CATEGORY_TO_SECTION).union(ic_frame["category"].dropna())):
        frame = ic_frame[ic_frame["category"] == category].copy()
        if frame.empty:
            frame = pd.DataFrame(
                columns=[
                    "factor",
                    "category",
                    "horizon",
                    "n_dates",
                    "avg_coverage",
                    "rank_ic_mean",
                    "rank_ic_t",
                    "qualified_for_research_bool",
                ]
            )
        category_rows.append(
            _score_category(
                str(category),
                frame,
                feature_summary,
                contribution_scores.get(str(category), 0.0),
            )
        )
    category_rows = sorted(
        category_rows,
        key=lambda row: (row["readiness_score"], row["max_n_dates"]),
        reverse=True,
    )

    blockers = _production_blockers(
        feature_summary=feature_summary,
        ic_report=ic_report,
        category_rows=category_rows,
        portfolio=portfolio,
    )
    production_ready = _production_ready(portfolio, blockers)
    report = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V23-alpha-readiness-scorecard",
        "research_only": True,
        "production_ready": production_ready,
        "gates": {
            "sharpe": PRODUCTION_SHARPE_GATE,
            "max_drawdown": PRODUCTION_MDD_GATE,
            "win_rate": PRODUCTION_WIN_RATE_GATE,
            "min_qualified_signals": MIN_PRODUCTION_QUALIFIED_SIGNALS,
            "min_production_dates": MIN_PRODUCTION_DATES,
            "min_production_avg_coverage": MIN_PRODUCTION_COVERAGE,
        },
        "evidence_files": {
            "feature_summary": str(feature_summary_path),
            "ic_json": str(ic_json_path),
            "ic_csv": str(ic_csv_path),
            "risk_grid": str(risk_grid_path),
            "crisis_grid": str(crisis_grid_path),
        },
        "archive": {
            "archive_mode": bool(feature_summary.get("archive_mode")),
            "archive_date": feature_summary.get("archive_date"),
            "archive_manifest_sha256": feature_summary.get("archive_manifest_sha256"),
            "archive_complete_for_builder": bool(
                feature_summary.get("archive_complete_for_builder")
            ),
            "live_fallback_files": list(feature_summary.get("live_fallback_files") or []),
            "stock_feature_rows": feature_summary.get("stock_feature_rows"),
            "stock_feature_symbols": feature_summary.get("stock_feature_symbols"),
            "stock_feature_dates": feature_summary.get("stock_feature_dates"),
            "crisis_asset_rows": feature_summary.get("crisis_asset_rows"),
            "crisis_assets": feature_summary.get("crisis_assets"),
            "stock_sections": feature_summary.get("stock_sections"),
        },
        "ic_summary": {
            "feature_rows": ic_report.get("feature_rows"),
            "feature_symbols": ic_report.get("feature_symbols"),
            "feature_dates": ic_report.get("feature_dates"),
            "qualified_count": ic_report.get("qualified_count"),
            "min_dates": ic_report.get("min_dates"),
            "min_cross_section": ic_report.get("min_cross_section"),
            "signal_lag_days": ic_report.get("signal_lag_days"),
            "split_date": ic_report.get("split_date"),
        },
        "category_readiness": category_rows,
        "portfolio": portfolio,
        "production_blockers": blockers,
        "next_required_work": [
            "accumulate or procure long PIT order-flow, fund-flow, northbound, "
            "margin/borrow and intraday panels",
            "redesign alpha on new PIT panels before more overlay parameter tuning",
            "obtain external WORM or CI artifact attestation for V21/V23 evidence",
            "connect broker/exchange-backed borrow, hedge, futures rollover and "
            "position-reconciliation providers before any live gate",
            "run paper trading for the required acceptance period after a strategy "
            "passes local backtest and OOS gates",
        ],
    }

    output_json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    pd.DataFrame(category_rows).to_csv(output_csv_path, index=False)
    print(
        "V23 alpha readiness: "
        f"production_ready={production_ready} "
        f"best_sharpe={portfolio.get('best', {}).get('sharpe_ratio')} "
        f"qualified_signals={ic_report.get('qualified_count')} "
        f"blockers={len(blockers)}",
        flush=True,
    )
    print(f"Wrote {output_json_path}", flush=True)
    print(f"Wrote {output_csv_path}", flush=True)
    if args.require_production_ready and not production_ready:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
