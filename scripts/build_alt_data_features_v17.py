#!/usr/bin/env python3
"""Build V17 daily features from the newly added alternative data sources."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from _paths import (
    ALT_ARCHIVES_DIR,
    ALT_FEATURES_DIR,
    ANALYST_EXPECTATIONS_DIR,
    FLOW_SIGNALS_DIR,
    HEDGE_ASSETS_DIR,
    INTRADAY_DIR,
    STOCK_LIST,
)

ALT_FEATURES_DIR.mkdir(parents=True, exist_ok=True)

ALLOW_MISSING = os.getenv("QUANT_ALT_FEATURES_ALLOW_MISSING", "1") == "1"
FILTER_TO_STOCK_LIST = os.getenv("QUANT_ALT_FEATURES_FILTER_STOCK_LIST", "1") == "1"
USE_ALT_ARCHIVE = os.getenv("QUANT_ALT_FEATURES_USE_ARCHIVE", "0") == "1"
ALT_ARCHIVE_DATE = os.getenv("QUANT_ALT_FEATURES_ARCHIVE_DATE", "").strip()
OUTPUT_PREFIX = os.getenv("QUANT_ALT_FEATURES_OUTPUT_PREFIX", "v17").strip() or "v17"
if "/" in OUTPUT_PREFIX or "\\" in OUTPUT_PREFIX:
    raise ValueError("QUANT_ALT_FEATURES_OUTPUT_PREFIX must be a filename prefix, not a path")

SNAPSHOT_ARCHIVE_SOURCES = {"flow_signals", "intraday"}
ARCHIVED_SOURCES_USED: set[str] = set()
ARCHIVED_FILES_USED: list[str] = []
LIVE_FALLBACK_FILES: list[str] = []


def _resolve_archive_root() -> Path | None:
    if not USE_ALT_ARCHIVE:
        return None
    if ALT_ARCHIVE_DATE:
        archive_date = ALT_ARCHIVE_DATE
    else:
        candidates = sorted(path for path in ALT_ARCHIVES_DIR.glob("*") if path.is_dir())
        if not candidates:
            raise FileNotFoundError(f"no alternative-data archive exists under {ALT_ARCHIVES_DIR}")
        archive_date = candidates[-1].name
    try:
        datetime.strptime(archive_date, "%Y%m%d")
    except ValueError as exc:
        raise ValueError("QUANT_ALT_FEATURES_ARCHIVE_DATE must be YYYYMMDD") from exc
    root = ALT_ARCHIVES_DIR / archive_date
    if not root.exists():
        raise FileNotFoundError(f"alternative-data archive does not exist: {root}")
    return root


ARCHIVE_ROOT = _resolve_archive_root()


def _archive_manifest_sha256() -> str | None:
    if ARCHIVE_ROOT is None:
        return None
    hash_path = ARCHIVE_ROOT / "manifest.sha256"
    if not hash_path.exists():
        return None
    return hash_path.read_text(encoding="utf-8").strip().split()[0]


def _source_file(source: str, live_dir: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if ARCHIVE_ROOT is None:
        return live_dir / relative
    archived = ARCHIVE_ROOT / source / relative
    if archived.exists():
        ARCHIVED_SOURCES_USED.add(source)
        ARCHIVED_FILES_USED.append(f"{source}/{relative.as_posix()}")
        return archived
    if source in SNAPSHOT_ARCHIVE_SOURCES:
        raise FileNotFoundError(
            f"archive mode requires snapshot source file: {archived}. "
            "Run scripts/archive_alt_data_snapshots.py for this archive date first."
        )
    LIVE_FALLBACK_FILES.append(f"{source}/{relative.as_posix()}")
    return live_dir / relative


def _read_parquet(path: Path, *, required: bool = False) -> pd.DataFrame:
    if not path.exists():
        if required and not ALLOW_MISSING:
            raise FileNotFoundError(path)
        return pd.DataFrame()
    return pd.read_parquet(path)


def _write_atomically(path: Path, df: pd.DataFrame) -> None:
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        df.to_parquet(tmp_path)
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _as_date(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.normalize()


def _safe_numeric(series: pd.Series | None) -> pd.Series:
    if series is None:
        return pd.Series(dtype=float)
    return pd.to_numeric(series, errors="coerce")


def _safe_divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    denominator = denominator.replace(0, np.nan)
    return numerator / denominator


def _load_universe_codes() -> set[str]:
    if not FILTER_TO_STOCK_LIST or not STOCK_LIST.exists():
        return set()
    raw = json.loads(STOCK_LIST.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise RuntimeError(f"stock list must be a JSON array: {STOCK_LIST}")
    return {str(item).zfill(6) for item in raw}


def _normalize_pct(series: pd.Series) -> pd.Series:
    values = _safe_numeric(series)
    return values.where(values.abs() <= 1.0, values / 100.0)


def build_analyst_snapshot_features() -> pd.DataFrame:
    df = _read_parquet(
        _source_file("analyst_expectations", ANALYST_EXPECTATIONS_DIR, "profit_forecast_em.parquet")
    )
    if df.empty:
        return pd.DataFrame()
    out = pd.DataFrame()
    out["code"] = df["code"].astype(str).str.zfill(6)
    out["date"] = _as_date(df["asof_date"])
    rating_cols = [column for column in df.columns if column.startswith("rating_count_")]
    rating_sum = sum((_safe_numeric(df[column]) for column in rating_cols), start=pd.Series(0.0, index=df.index))
    buy_plus = _safe_numeric(df.get("rating_count_买入")) + _safe_numeric(df.get("rating_count_增持"))
    sell_plus = _safe_numeric(df.get("rating_count_减持")) + _safe_numeric(df.get("rating_count_卖出"))
    out["analyst_report_count_6m"] = _safe_numeric(df.get("report_count_6m"))
    out["analyst_positive_rating_ratio"] = _safe_divide(buy_plus, rating_sum)
    out["analyst_negative_rating_ratio"] = _safe_divide(sell_plus, rating_sum)
    out["analyst_consensus_eps_2025"] = _safe_numeric(df.get("forecast_eps_2025"))
    out["analyst_consensus_eps_2026"] = _safe_numeric(df.get("forecast_eps_2026"))
    out["analyst_consensus_eps_2027"] = _safe_numeric(df.get("forecast_eps_2027"))
    out["analyst_eps_growth_26_vs_25"] = _safe_divide(
        out["analyst_consensus_eps_2026"] - out["analyst_consensus_eps_2025"],
        out["analyst_consensus_eps_2025"].abs(),
    )
    out["analyst_eps_growth_27_vs_26"] = _safe_divide(
        out["analyst_consensus_eps_2027"] - out["analyst_consensus_eps_2026"],
        out["analyst_consensus_eps_2026"].abs(),
    )
    out["feature_source_analyst_snapshot"] = "eastmoney_profit_forecast_akshare"
    return out.dropna(subset=["code", "date"]).drop_duplicates(["code", "date"], keep="last")


def build_analyst_revision_features() -> pd.DataFrame:
    df = _read_parquet(
        _source_file("analyst_expectations", ANALYST_EXPECTATIONS_DIR, "analyst_ratings_cninfo.parquet")
    )
    if df.empty:
        return pd.DataFrame()
    working = df.copy()
    working["code"] = working["code"].astype(str).str.zfill(6)
    working["date"] = _as_date(working["publish_date"])
    working["upgrade"] = (_safe_numeric(working.get("revision_score")) > 0).astype(float)
    working["downgrade"] = (_safe_numeric(working.get("revision_score")) < 0).astype(float)
    grouped = working.groupby(["code", "date"], as_index=False).agg(
        analyst_rating_events=("rating", "count"),
        analyst_rating_score_mean=("rating_score", "mean"),
        analyst_revision_score_sum=("revision_score", "sum"),
        analyst_upgrade_count=("upgrade", "sum"),
        analyst_downgrade_count=("downgrade", "sum"),
        analyst_target_price_low_mean=("target_price_low", "mean"),
        analyst_target_price_high_mean=("target_price_high", "mean"),
    )
    grouped["analyst_upgrade_minus_downgrade"] = (
        grouped["analyst_upgrade_count"] - grouped["analyst_downgrade_count"]
    )
    grouped["feature_source_analyst_revision"] = "cninfo_stock_rank_forecast_akshare"
    return grouped


def build_northbound_features() -> pd.DataFrame:
    df = _read_parquet(_source_file("flow_signals", FLOW_SIGNALS_DIR, "northbound_holdings.parquet"))
    if df.empty:
        return pd.DataFrame()
    out = pd.DataFrame()
    out["code"] = df["code"].astype(str).str.zfill(6)
    out["date"] = _as_date(df["date"] if "date" in df.columns else df["持股日期"])
    hold_pct = df.get("持股数量占A股百分比_numeric", df.get("持股数量占A股百分比"))
    out["northbound_hold_pct"] = _normalize_pct(_safe_numeric(hold_pct))
    out["northbound_holding_shares"] = _safe_numeric(df.get("持股数量_numeric", df.get("持股数量")))
    out["northbound_share_delta"] = _safe_numeric(df.get("今日增持股数"))
    out["northbound_net_buy_amount"] = _safe_numeric(df.get("今日增持资金"))
    out["northbound_market_value_change"] = _safe_numeric(df.get("今日持股市值变化"))
    out = out.dropna(subset=["code", "date"]).drop_duplicates(["code", "date"], keep="last")
    out["northbound_hold_pct_change_5d"] = out.groupby("code")["northbound_hold_pct"].diff(5)
    out["feature_source_northbound"] = "eastmoney_hsgt_individual_akshare"
    return out


def build_fund_flow_rank_features() -> pd.DataFrame:
    df = _read_parquet(_source_file("flow_signals", FLOW_SIGNALS_DIR, "fund_flow_ranks.parquet"))
    if df.empty:
        return pd.DataFrame()
    working = df.copy()
    working["code"] = working["code"].astype(str).str.zfill(6)
    working["date"] = _as_date(working["asof_date"])
    frames: list[pd.DataFrame] = []
    for horizon, group in working.groupby("horizon"):
        suffix = str(horizon).replace("日排行", "d").replace(" ", "")
        frame = pd.DataFrame()
        frame["code"] = group["code"]
        frame["date"] = group["date"]
        frame[f"fund_flow_net_{suffix}"] = _safe_numeric(group.get("资金流入净额_numeric"))
        frame[f"fund_flow_turnover_{suffix}"] = _normalize_pct(group.get("连续换手率_numeric"))
        frame[f"fund_flow_return_{suffix}"] = _normalize_pct(group.get("阶段涨跌幅_numeric"))
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    out = frames[0]
    for frame in frames[1:]:
        out = out.merge(frame, on=["code", "date"], how="outer")
    out["feature_source_fund_flow_rank"] = "eastmoney_fund_flow_rank_akshare"
    return out.drop_duplicates(["code", "date"], keep="last")


def build_big_deal_features() -> pd.DataFrame:
    df = _read_parquet(_source_file("flow_signals", FLOW_SIGNALS_DIR, "big_deal_current.parquet"))
    if df.empty:
        return pd.DataFrame()
    working = df.copy()
    working["code"] = working["code"].astype(str).str.zfill(6)
    working["date"] = _as_date(working["asof_date"])
    working["amount"] = _safe_numeric(working.get("成交额_numeric"))
    working["is_buy"] = working.get("大单性质", "").astype(str).str.contains("买").astype(float)
    working["is_sell"] = working.get("大单性质", "").astype(str).str.contains("卖").astype(float)
    working["buy_amount"] = working["amount"] * working["is_buy"]
    working["sell_amount"] = working["amount"] * working["is_sell"]
    out = working.groupby(["code", "date"], as_index=False).agg(
        big_deal_count=("amount", "count"),
        big_deal_amount=("amount", "sum"),
        big_deal_buy_amount=("buy_amount", "sum"),
        big_deal_sell_amount=("sell_amount", "sum"),
    )
    out["big_deal_buy_sell_imbalance"] = _safe_divide(
        out["big_deal_buy_amount"] - out["big_deal_sell_amount"],
        out["big_deal_buy_amount"] + out["big_deal_sell_amount"],
    )
    out["feature_source_big_deal"] = "eastmoney_big_deal_current_akshare"
    return out


def build_margin_features() -> pd.DataFrame:
    df = _read_parquet(_source_file("flow_signals", FLOW_SIGNALS_DIR, "margin_details.parquet"))
    if df.empty:
        return pd.DataFrame()
    out = pd.DataFrame()
    out["code"] = df["code"].astype(str).str.extract(r"(\d{6})", expand=False).str.zfill(6)
    out["date"] = _as_date(df["date"])
    out["margin_financing_balance"] = _safe_numeric(df.get("融资余额_numeric"))
    out["margin_financing_buy"] = _safe_numeric(df.get("融资买入额_numeric"))
    out["margin_financing_repay"] = _safe_numeric(df.get("融资偿还额_numeric"))
    out["margin_short_sell_volume"] = _safe_numeric(df.get("融券卖出量_numeric"))
    out["margin_short_balance_volume"] = _safe_numeric(df.get("融券余量_numeric"))
    out["margin_short_balance"] = _safe_numeric(df.get("融券余额_numeric"))
    out["feature_source_margin"] = "sse_szse_margin_detail_akshare"
    out = out.dropna(subset=["code", "date"]).drop_duplicates(["code", "date"], keep="last")
    out["margin_financing_balance_change_5d"] = out.groupby("code")[
        "margin_financing_balance"
    ].diff(5)
    return out


def build_intraday_features() -> pd.DataFrame:
    df = _read_parquet(_source_file("intraday", INTRADAY_DIR, "microstructure_features.parquet"))
    if df.empty:
        return pd.DataFrame()
    keep = [
        "code",
        "date",
        "bars",
        "intraday_return",
        "intraday_reversal_alpha",
        "first_hour_return",
        "realized_vol_5m",
        "amihud_5m",
        "close_vs_vwap",
        "range_position",
        "first_hour_volume_share",
        "last_hour_volume_share",
        "turnover_amount",
        "turnover_volume",
    ]
    out = df[[column for column in keep if column in df.columns]].copy()
    out["code"] = out["code"].astype(str).str.zfill(6)
    out["date"] = _as_date(out["date"])
    out["feature_source_intraday"] = "sina_stock_zh_a_minute_akshare"
    return out.dropna(subset=["code", "date"]).drop_duplicates(["code", "date"], keep="last")


def merge_stock_features(frames: list[pd.DataFrame]) -> pd.DataFrame:
    usable = [frame for frame in frames if not frame.empty]
    if not usable:
        return pd.DataFrame()
    merged = usable[0]
    for frame in usable[1:]:
        merged = merged.merge(frame, on=["code", "date"], how="outer")
    universe_codes = _load_universe_codes()
    if universe_codes:
        merged = merged[merged["code"].isin(universe_codes)]
    merged = merged.sort_values(["date", "code"]).reset_index(drop=True)
    return merged


def build_crisis_asset_features() -> pd.DataFrame:
    df = _read_parquet(_source_file("hedge_assets", HEDGE_ASSETS_DIR, "hedge_crisis_assets.parquet"))
    if df.empty:
        return pd.DataFrame()
    working = df.reset_index().rename(columns={"index": "date"}).copy()
    working["date"] = _as_date(working["date"])
    working = working.sort_values(["asset_id", "date"])
    rows: list[pd.DataFrame] = []
    for _asset_id, group in working.groupby("asset_id", sort=True):
        group = group.copy()
        close = _safe_numeric(group["close"])
        returns = close.pct_change()
        group["asset_return_1d"] = returns
        group["asset_momentum_20d"] = close.pct_change(20)
        group["asset_momentum_60d"] = close.pct_change(60)
        group["asset_realized_vol_20d"] = returns.rolling(20).std(ddof=0) * np.sqrt(252)
        group["asset_drawdown_60d"] = close / close.rolling(60).max() - 1.0
        group["asset_trend_score"] = (
            group["asset_momentum_20d"].rank(pct=True)
            if len(group["asset_momentum_20d"].dropna()) > 1
            else np.nan
        )
        rows.append(group)
    out = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    keep = [
        "date",
        "asset_id",
        "symbol",
        "name",
        "role",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "open_interest",
        "amount",
        "asset_return_1d",
        "asset_momentum_20d",
        "asset_momentum_60d",
        "asset_realized_vol_20d",
        "asset_drawdown_60d",
        "asset_trend_score",
        "source",
    ]
    return out[[column for column in keep if column in out.columns]].sort_values(["date", "asset_id"])


def main() -> None:
    stock_sections: dict[str, int] = {}
    builders: list[tuple[str, Any]] = [
        ("analyst_snapshot", build_analyst_snapshot_features),
        ("analyst_revision", build_analyst_revision_features),
        ("northbound", build_northbound_features),
        ("fund_flow_rank", build_fund_flow_rank_features),
        ("big_deal", build_big_deal_features),
        ("margin", build_margin_features),
        ("intraday", build_intraday_features),
    ]
    stock_frames: list[pd.DataFrame] = []
    for name, builder in builders:
        frame = builder()
        stock_sections[name] = len(frame)
        if not frame.empty:
            stock_frames.append(frame)
    stock_features = merge_stock_features(stock_frames)
    crisis_features = build_crisis_asset_features()
    stock_output = ALT_FEATURES_DIR / f"{OUTPUT_PREFIX}_stock_alt_features.parquet"
    crisis_output = ALT_FEATURES_DIR / f"{OUTPUT_PREFIX}_crisis_asset_features.parquet"
    summary_output = ALT_FEATURES_DIR / f"{OUTPUT_PREFIX}_alt_feature_summary.json"
    _write_atomically(stock_output, stock_features)
    _write_atomically(crisis_output, crisis_features)
    summary = {
        "timestamp": datetime.now().isoformat(),
        "output_prefix": OUTPUT_PREFIX,
        "stock_output": str(stock_output),
        "crisis_output": str(crisis_output),
        "stock_feature_rows": len(stock_features),
        "stock_feature_symbols": int(stock_features["code"].nunique()) if not stock_features.empty else 0,
        "stock_feature_dates": int(stock_features["date"].nunique()) if not stock_features.empty else 0,
        "filtered_to_stock_list": FILTER_TO_STOCK_LIST,
        "stock_list": str(STOCK_LIST) if FILTER_TO_STOCK_LIST else None,
        "stock_sections": stock_sections,
        "archive_mode": USE_ALT_ARCHIVE,
        "archive_root": str(ARCHIVE_ROOT) if ARCHIVE_ROOT is not None else None,
        "archive_date": ARCHIVE_ROOT.name if ARCHIVE_ROOT is not None else None,
        "archive_manifest_sha256": _archive_manifest_sha256(),
        "archive_required_sources": sorted(SNAPSHOT_ARCHIVE_SOURCES),
        "archived_sources_used": sorted(ARCHIVED_SOURCES_USED),
        "archived_files_used": sorted(set(ARCHIVED_FILES_USED)),
        "live_fallback_files": sorted(set(LIVE_FALLBACK_FILES)),
        "archive_complete_for_builder": USE_ALT_ARCHIVE and not LIVE_FALLBACK_FILES,
        "crisis_asset_rows": len(crisis_features),
        "crisis_assets": (
            sorted(crisis_features["asset_id"].dropna().unique().tolist())
            if not crisis_features.empty
            else []
        ),
        "pit_warning": (
            "Use analyst ratings, northbound holdings, margin and intraday rows by their own dates. "
            "When QUANT_ALT_FEATURES_USE_ARCHIVE=1, flow_signals and intraday snapshot sources "
            "are loaded from the requested archive date and bound to archive_manifest_sha256. "
            "Non-archived sources listed in live_fallback_files remain research-only live inputs."
        ),
    }
    summary_output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(
        f"Built V17 alt features: stock_rows={len(stock_features)} "
        f"symbols={summary['stock_feature_symbols']} crisis_rows={len(crisis_features)} "
        f"output_prefix={OUTPUT_PREFIX}",
        flush=True,
    )


if __name__ == "__main__":
    main()
