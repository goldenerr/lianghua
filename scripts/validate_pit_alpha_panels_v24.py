#!/usr/bin/env python3
"""Validate point-in-time alpha data panels for V24 research readiness.

This is a data gate, not a strategy gate. It classifies each requested new
information source as research-ready, snapshot-only, coverage-limited, missing,
or still blocked by production evidence requirements.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from _paths import (
    ALT_FEATURES_DIR,
    ANALYST_EXPECTATIONS_DIR,
    FLOW_SIGNALS_DIR,
    HEDGE_ASSETS_DIR,
    INTRADAY_DIR,
    PROJECT_DIR,
    RESULTS_DIR,
    STOCK_LIST,
)

DEFAULT_FEATURE_SUMMARY = "v21_archive_20260608_alt_feature_summary.json"
DEFAULT_OUTPUT_JSON = "quant_pit_alpha_panel_readiness_v24.json"
DEFAULT_OUTPUT_CSV = "quant_pit_alpha_panel_readiness_v24.csv"
DEFAULT_MIN_STOCK_TARGET_SYMBOLS = int(os.getenv("QUANT_PIT_MIN_STOCK_TARGET_SYMBOLS", "2000"))
DEFAULT_MIN_STOCK_OVERLAP_RATIO = float(os.getenv("QUANT_PIT_MIN_STOCK_OVERLAP_RATIO", "0.95"))


@dataclass(frozen=True)
class PanelSpec:
    panel_id: str
    source_group: str
    path: Path
    date_column: str
    entity_column: str | None
    entity_type: str
    min_dates: int
    min_entity_count: int
    min_universe_overlap_ratio: float
    snapshot_source: bool
    production_evidence_required: str
    note: str


def _panel_specs() -> list[PanelSpec]:
    min_stock_symbols = DEFAULT_MIN_STOCK_TARGET_SYMBOLS
    min_stock_overlap = DEFAULT_MIN_STOCK_OVERLAP_RATIO
    return [
        PanelSpec(
            panel_id="analyst_consensus_snapshot",
            source_group="analyst_expectations",
            path=ANALYST_EXPECTATIONS_DIR / "profit_forecast_em.parquet",
            date_column="asof_date",
            entity_column="code",
            entity_type="stock",
            min_dates=500,
            min_entity_count=min_stock_symbols,
            min_universe_overlap_ratio=min_stock_overlap,
            snapshot_source=True,
            production_evidence_required="paid PIT consensus provider or daily WORM snapshots",
            note="current EastMoney consensus snapshot; not a historical PIT panel",
        ),
        PanelSpec(
            panel_id="analyst_revision_history",
            source_group="analyst_expectations",
            path=ANALYST_EXPECTATIONS_DIR / "analyst_ratings_cninfo.parquet",
            date_column="publish_date",
            entity_column="code",
            entity_type="stock",
            min_dates=500,
            min_entity_count=min_stock_symbols,
            min_universe_overlap_ratio=min_stock_overlap,
            snapshot_source=False,
            production_evidence_required="external WORM/provider entitlement evidence",
            note="dated CNInfo rating/revision events with publish-date PIT semantics",
        ),
        PanelSpec(
            panel_id="northbound_stock_holding_history",
            source_group="flow_signals",
            path=FLOW_SIGNALS_DIR / "northbound_holdings.parquet",
            date_column="date",
            entity_column="code",
            entity_type="stock",
            min_dates=1000,
            min_entity_count=min_stock_symbols,
            min_universe_overlap_ratio=min_stock_overlap,
            snapshot_source=False,
            production_evidence_required="full-universe provider entitlement evidence",
            note="true history exists for fetched names, but current file is sample-limited",
        ),
        PanelSpec(
            panel_id="northbound_market_aggregate_history",
            source_group="flow_signals",
            path=FLOW_SIGNALS_DIR / "northbound_aggregate.parquet",
            date_column="date",
            entity_column=None,
            entity_type="market_aggregate",
            min_dates=1000,
            min_entity_count=1,
            min_universe_overlap_ratio=0.0,
            snapshot_source=False,
            production_evidence_required="provider entitlement and archive attestation",
            note="market-level aggregate only; useful as regime input, not stock alpha alone",
        ),
        PanelSpec(
            panel_id="fund_flow_rank_snapshot",
            source_group="flow_signals",
            path=FLOW_SIGNALS_DIR / "fund_flow_ranks.parquet",
            date_column="asof_date",
            entity_column="code",
            entity_type="stock",
            min_dates=500,
            min_entity_count=min_stock_symbols,
            min_universe_overlap_ratio=min_stock_overlap,
            snapshot_source=True,
            production_evidence_required="daily WORM snapshots or historical PIT fund-flow feed",
            note="rank endpoint is a current snapshot; daily archive must accumulate history",
        ),
        PanelSpec(
            panel_id="big_deal_order_flow_snapshot",
            source_group="flow_signals",
            path=FLOW_SIGNALS_DIR / "big_deal_current.parquet",
            date_column="asof_date",
            entity_column="code",
            entity_type="stock",
            min_dates=250,
            min_entity_count=min_stock_symbols,
            min_universe_overlap_ratio=min_stock_overlap,
            snapshot_source=True,
            production_evidence_required="daily WORM snapshots or historical order-flow feed",
            note="big-deal endpoint is a current snapshot and must be archived daily",
        ),
        PanelSpec(
            panel_id="margin_short_daily_history",
            source_group="flow_signals",
            path=FLOW_SIGNALS_DIR / "margin_details.parquet",
            date_column="date",
            entity_column="code",
            entity_type="stock",
            min_dates=500,
            min_entity_count=min_stock_symbols,
            min_universe_overlap_ratio=min_stock_overlap,
            snapshot_source=False,
            production_evidence_required="exchange-backed margin/short provider evidence",
            note="current fetch covers only the configured margin date window",
        ),
        PanelSpec(
            panel_id="intraday_microstructure_history",
            source_group="intraday",
            path=INTRADAY_DIR / "microstructure_features.parquet",
            date_column="date",
            entity_column="code",
            entity_type="stock",
            min_dates=250,
            min_entity_count=min_stock_symbols,
            min_universe_overlap_ratio=min_stock_overlap,
            snapshot_source=True,
            production_evidence_required="historical minute feed or long daily WORM archive",
            note="Sina minute endpoint exposes a rolling window; current file is sample-limited",
        ),
        PanelSpec(
            panel_id="hedge_crisis_asset_history",
            source_group="hedge_assets",
            path=HEDGE_ASSETS_DIR / "hedge_crisis_assets.parquet",
            date_column="date",
            entity_column="asset_id",
            entity_type="asset",
            min_dates=500,
            min_entity_count=10,
            min_universe_overlap_ratio=0.0,
            snapshot_source=False,
            production_evidence_required=(
                "broker/exchange-backed tradability, rollover, margin and position evidence"
            ),
            note="research proxy history exists; live hedge execution evidence is absent",
        ),
        PanelSpec(
            panel_id="exchange_borrow_availability",
            source_group="borrow",
            path=PROJECT_DIR / "data" / "borrow" / "short_availability.parquet",
            date_column="date",
            entity_column="code",
            entity_type="stock",
            min_dates=250,
            min_entity_count=min_stock_symbols,
            min_universe_overlap_ratio=min_stock_overlap,
            snapshot_source=False,
            production_evidence_required="broker/exchange-backed borrow availability feed",
            note="required for real market-neutral/short sleeve; no local provider file exists",
        ),
    ]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--feature-summary",
        default=os.getenv("QUANT_V24_FEATURE_SUMMARY", DEFAULT_FEATURE_SUMMARY),
        help="Feature summary JSON path or filename under data/alt_features.",
    )
    parser.add_argument(
        "--output-json",
        default=os.getenv("QUANT_V24_OUTPUT_JSON", DEFAULT_OUTPUT_JSON),
        help="Output JSON path or filename under data/backtest_results.",
    )
    parser.add_argument(
        "--output-csv",
        default=os.getenv("QUANT_V24_OUTPUT_CSV", DEFAULT_OUTPUT_CSV),
        help="Output CSV path or filename under data/backtest_results.",
    )
    parser.add_argument(
        "--require-production-ready",
        action="store_true",
        help="Exit non-zero when any production panel is not ready.",
    )
    return parser.parse_args()


def _resolve(path_value: str, default_dir: Path) -> Path:
    path = Path(path_value)
    if path.is_absolute() or path.parent != Path("."):
        return path
    return default_dir / path


def _load_universe() -> set[str]:
    if not STOCK_LIST.exists():
        return set()
    raw = json.loads(STOCK_LIST.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return set()
    return {str(item).zfill(6) for item in raw if isinstance(item, str)}


def _universe_sha256() -> str | None:
    if not STOCK_LIST.exists():
        return None
    raw = json.loads(STOCK_LIST.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return None
    normalized = [str(item).zfill(6) for item in raw if isinstance(item, str)]
    payload = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_feature_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"summary_missing": True}
    return json.loads(path.read_text(encoding="utf-8"))


def _load_frame(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_parquet(path)


def _date_series(frame: pd.DataFrame, date_column: str) -> pd.Series:
    if frame.empty:
        return pd.Series(dtype="datetime64[ns]")
    if date_column in frame.columns:
        return pd.to_datetime(frame[date_column], errors="coerce").dt.normalize()
    if frame.index.name == date_column or isinstance(frame.index, pd.DatetimeIndex):
        return pd.to_datetime(pd.Series(frame.index, index=frame.index), errors="coerce").dt.normalize()
    return pd.Series(dtype="datetime64[ns]")


def _entity_series(frame: pd.DataFrame, entity_column: str | None) -> pd.Series:
    if frame.empty or entity_column is None or entity_column not in frame.columns:
        return pd.Series(dtype="object")
    values = frame[entity_column].astype(str)
    if entity_column == "code":
        values = values.str.extract(r"(\d{6})", expand=False).str.zfill(6)
    return values


def _median_entities_per_date(
    frame: pd.DataFrame,
    dates: pd.Series,
    entities: pd.Series,
) -> float | None:
    if frame.empty or dates.empty or entities.empty:
        return None
    working = pd.DataFrame({"panel_date": dates, "entity": entities}).reset_index(drop=True)
    working = working.dropna(subset=["panel_date", "entity"])
    if working.empty:
        return None
    return float(working.groupby("panel_date")["entity"].nunique().median())


def _status(
    *,
    exists: bool,
    unique_dates: int,
    unique_entities: int,
    universe_overlap_ratio: float | None,
    archived: bool,
    spec: PanelSpec,
) -> tuple[str, bool, bool, list[str]]:
    failures: list[str] = []
    if not exists:
        failures.append("missing_file")
    if exists and unique_dates < spec.min_dates:
        failures.append(f"unique_dates<{spec.min_dates}")
    if exists and unique_entities < spec.min_entity_count:
        failures.append(f"unique_entities<{spec.min_entity_count}")
    if (
        exists
        and spec.entity_type == "stock"
        and universe_overlap_ratio is not None
        and universe_overlap_ratio < spec.min_universe_overlap_ratio
    ):
        failures.append(f"universe_overlap<{spec.min_universe_overlap_ratio:.2f}")
    if exists and not archived:
        failures.append("not_archive_bound")

    research_ready = exists and not any(
        item.startswith(("unique_dates", "unique_entities", "universe_overlap", "missing"))
        for item in failures
    )
    production_ready = research_ready and False
    if not exists:
        status = "missing"
    elif spec.snapshot_source and unique_dates <= 5:
        status = "snapshot_only_not_historical"
    elif not research_ready:
        status = "insufficient_history_or_coverage"
    else:
        status = "research_ready_external_evidence_missing"
    return status, research_ready, production_ready, failures


def _score_panel(
    spec: PanelSpec,
    *,
    universe: set[str],
    archived_sources: set[str],
) -> dict[str, Any]:
    frame = _load_frame(spec.path)
    exists = spec.path.exists() and not frame.empty
    dates = _date_series(frame, spec.date_column)
    dates = dates.dropna()
    entities = _entity_series(frame, spec.entity_column)
    unique_dates = int(dates.nunique()) if not dates.empty else 0
    if spec.entity_column is None and exists:
        unique_entities = 1
    else:
        unique_entities = int(entities.dropna().nunique()) if not entities.empty else 0
    date_start = dates.min().date().isoformat() if not dates.empty else None
    date_end = dates.max().date().isoformat() if not dates.empty else None
    universe_overlap = 0
    universe_overlap_ratio: float | None = None
    if spec.entity_type == "stock" and universe:
        universe_overlap = len(set(entities.dropna()).intersection(universe))
        universe_overlap_ratio = universe_overlap / len(universe)
    median_entities = _median_entities_per_date(frame, dates, entities)
    archived = spec.source_group in archived_sources
    status, research_ready, production_ready, failures = _status(
        exists=exists,
        unique_dates=unique_dates,
        unique_entities=unique_entities,
        universe_overlap_ratio=universe_overlap_ratio,
        archived=archived,
        spec=spec,
    )
    return {
        "panel_id": spec.panel_id,
        "source_group": spec.source_group,
        "path": str(spec.path),
        "exists": exists,
        "rows": int(len(frame)) if exists else 0,
        "date_start": date_start,
        "date_end": date_end,
        "unique_dates": unique_dates,
        "entity_type": spec.entity_type,
        "unique_entities": unique_entities,
        "universe_overlap": universe_overlap if spec.entity_type == "stock" else None,
        "universe_overlap_ratio": round(universe_overlap_ratio, 4)
        if universe_overlap_ratio is not None
        else None,
        "median_entities_per_date": round(median_entities, 2)
        if median_entities is not None
        else None,
        "min_dates": spec.min_dates,
        "min_entity_count": spec.min_entity_count,
        "min_universe_overlap_ratio": spec.min_universe_overlap_ratio,
        "snapshot_source": spec.snapshot_source,
        "archive_bound": archived,
        "research_panel_ready": research_ready,
        "production_panel_ready": production_ready,
        "status": status,
        "failures": failures,
        "production_evidence_required": spec.production_evidence_required,
        "note": spec.note,
    }


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total_panels": len(rows),
        "research_ready_panels": sum(bool(row["research_panel_ready"]) for row in rows),
        "production_ready_panels": sum(bool(row["production_panel_ready"]) for row in rows),
        "missing_panels": [row["panel_id"] for row in rows if row["status"] == "missing"],
        "snapshot_only_panels": [
            row["panel_id"] for row in rows if row["status"] == "snapshot_only_not_historical"
        ],
        "coverage_or_history_gaps": [
            row["panel_id"]
            for row in rows
            if row["status"] == "insufficient_history_or_coverage"
        ],
        "research_ready_external_evidence_missing": [
            row["panel_id"]
            for row in rows
            if row["status"] == "research_ready_external_evidence_missing"
        ],
    }


def main() -> None:
    args = _parse_args()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    feature_summary_path = _resolve(str(args.feature_summary), ALT_FEATURES_DIR)
    output_json_path = _resolve(str(args.output_json), RESULTS_DIR)
    output_csv_path = _resolve(str(args.output_csv), RESULTS_DIR)
    feature_summary = _load_feature_summary(feature_summary_path)
    archived_sources = set(feature_summary.get("archived_sources_used") or [])
    universe = _load_universe()
    universe_too_small = len(universe) < DEFAULT_MIN_STOCK_TARGET_SYMBOLS
    rows = [
        _score_panel(spec, universe=universe, archived_sources=archived_sources)
        for spec in _panel_specs()
    ]
    summary = _summarize(rows)
    production_ready = summary["production_ready_panels"] == summary["total_panels"]
    report = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "V24-pit-alpha-panel-readiness",
        "research_only": True,
        "production_data_ready": production_ready,
        "feature_summary": {
            "path": str(feature_summary_path),
            "archive_date": feature_summary.get("archive_date"),
            "archive_manifest_sha256": feature_summary.get("archive_manifest_sha256"),
            "archive_complete_for_builder": feature_summary.get("archive_complete_for_builder"),
            "archived_sources_used": sorted(archived_sources),
        },
        "target_universe": {
            "path": str(STOCK_LIST),
            "size": len(universe),
            "sha256": _universe_sha256(),
            "min_stock_target_symbols": DEFAULT_MIN_STOCK_TARGET_SYMBOLS,
            "min_stock_overlap_ratio": DEFAULT_MIN_STOCK_OVERLAP_RATIO,
            "passes_minimum_size": not universe_too_small,
        },
        "universe_size": len(universe),
        "summary": summary,
        "panels": rows,
        "production_blockers": [
            *(
                [
                    f"target universe has only {len(universe)} symbols; "
                    f"requires at least {DEFAULT_MIN_STOCK_TARGET_SYMBOLS}"
                ]
                if universe_too_small
                else []
            ),
            "no panel has production external evidence attached; local files are research evidence only",
            "snapshot-only panels need daily WORM accumulation or paid historical PIT feeds",
            (
                "stock-level northbound, margin/short, borrow and intraday panels must cover "
                f"about {DEFAULT_MIN_STOCK_TARGET_SYMBOLS} non-large-cap target names"
            ),
            "borrow/short availability feed is missing, blocking real market-neutral or short sleeves",
        ],
        "next_required_work": [
            "expand northbound individual holding fetch beyond sample mode or procure full PIT holdings",
            "run daily WORM snapshots for fund-flow, big-deal and intraday until history is usable",
            "backfill margin/short data across a multi-year date range where provider allows",
            "connect broker-backed borrow availability before designing real short/market-neutral sleeves",
            "attach external WORM/provider entitlement evidence before any production gate can pass",
        ],
    }
    output_json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    pd.DataFrame(rows).to_csv(output_csv_path, index=False)
    print(
        "V24 PIT panel readiness: "
        f"production_data_ready={production_ready} "
        f"research_ready_panels={summary['research_ready_panels']}/{summary['total_panels']} "
        f"snapshot_only={len(summary['snapshot_only_panels'])} "
        f"missing={len(summary['missing_panels'])}",
        flush=True,
    )
    print(f"Wrote {output_json_path}", flush=True)
    print(f"Wrote {output_csv_path}", flush=True)
    if args.require_production_ready and not production_ready:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
