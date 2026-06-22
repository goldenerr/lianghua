#!/usr/bin/env python3
"""V18 alternative-data factor IC and bucket-return diagnostics.

The goal is to test the V17 information sources before they are blended into a
portfolio. Signals are evaluated only from their own feature date plus a
conservative lag; current snapshots are never backfilled into prior history.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import validate_quant_logic_v5_9 as base
from _paths import ALT_FEATURES_DIR, ANALYST_EXPECTATIONS_DIR, RESULTS_DIR

RESULTS_DIR.mkdir(parents=True, exist_ok=True)

STOCK_ALT_FEATURE_FILE = os.getenv("QUANT_V18_STOCK_ALT_FEATURE_FILE", "v17_stock_alt_features.parquet")
ANALYST_RATINGS_FILE = os.getenv(
    "QUANT_V18_ANALYST_RATINGS_FILE",
    str(ANALYST_EXPECTATIONS_DIR / "analyst_ratings_cninfo.parquet"),
)
ALT_FEATURE_SUMMARY_FILE = os.getenv("QUANT_V18_ALT_FEATURE_SUMMARY_FILE", "").strip()
OUTPUT_SUFFIX = os.getenv("QUANT_V18_OUTPUT_SUFFIX", "").strip()
SIGNAL_LAG_DAYS = int(os.getenv("QUANT_V18_SIGNAL_LAG_DAYS", "1"))
MIN_CROSS_SECTION = int(os.getenv("QUANT_V18_MIN_CROSS_SECTION", "30"))
MIN_DATES = int(os.getenv("QUANT_V18_MIN_DATES", "60"))
TOP_BOTTOM_FRAC = float(os.getenv("QUANT_V18_TOP_BOTTOM_FRAC", "0.20"))
START_DATE = pd.Timestamp(os.getenv("QUANT_V18_START_DATE", "2024-01-01"))
SPLIT_DATE = pd.Timestamp(os.getenv("QUANT_V18_SPLIT_DATE", "2025-07-01"))
HORIZONS = tuple(
    int(item)
    for item in os.getenv("QUANT_V18_HORIZONS", "1,5,10,20").split(",")
    if item.strip()
)
ROLLING_WINDOWS = tuple(
    int(item)
    for item in os.getenv("QUANT_V18_ANALYST_ROLLING_WINDOWS", "20,60").split(",")
    if item.strip()
)


@dataclass(frozen=True)
class FactorSpec:
    name: str
    category: str
    description: str


FACTOR_SPECS = [
    FactorSpec("analyst_report_count_6m", "analyst_consensus", "recent analyst report count"),
    FactorSpec("analyst_positive_rating_ratio", "analyst_consensus", "positive rating ratio"),
    FactorSpec("analyst_negative_rating_ratio", "analyst_consensus", "negative rating ratio"),
    FactorSpec("analyst_eps_growth_26_vs_25", "analyst_consensus", "EPS growth 2026 vs 2025"),
    FactorSpec("analyst_eps_growth_27_vs_26", "analyst_consensus", "EPS growth 2027 vs 2026"),
    FactorSpec("analyst_rating_events", "analyst_revision", "daily rating event count"),
    FactorSpec("analyst_rating_score_mean", "analyst_revision", "mean rating score"),
    FactorSpec("analyst_revision_score_sum", "analyst_revision", "rating revision score sum"),
    FactorSpec("analyst_upgrade_count", "analyst_revision", "rating upgrade count"),
    FactorSpec("analyst_downgrade_count", "analyst_revision", "rating downgrade count"),
    FactorSpec("analyst_upgrade_minus_downgrade", "analyst_revision", "upgrade minus downgrade"),
    FactorSpec("northbound_hold_pct", "northbound", "northbound holding percentage"),
    FactorSpec("northbound_net_buy_amount", "northbound", "northbound net buy amount"),
    FactorSpec("northbound_hold_pct_change_5d", "northbound", "5-day northbound holding change"),
    FactorSpec("fund_flow_net_3d", "fund_flow", "3-day fund-flow net amount"),
    FactorSpec("fund_flow_turnover_3d", "fund_flow", "3-day fund-flow turnover"),
    FactorSpec("fund_flow_return_3d", "fund_flow", "3-day fund-flow return"),
    FactorSpec("fund_flow_net_5d", "fund_flow", "5-day fund-flow net amount"),
    FactorSpec("fund_flow_turnover_5d", "fund_flow", "5-day fund-flow turnover"),
    FactorSpec("fund_flow_return_5d", "fund_flow", "5-day fund-flow return"),
    FactorSpec("big_deal_count", "order_flow", "big deal count"),
    FactorSpec("big_deal_amount", "order_flow", "big deal amount"),
    FactorSpec("big_deal_buy_sell_imbalance", "order_flow", "big deal buy/sell imbalance"),
    FactorSpec("margin_financing_balance", "margin_short", "margin financing balance"),
    FactorSpec("margin_financing_buy", "margin_short", "margin financing buy amount"),
    FactorSpec("margin_short_sell_volume", "margin_short", "short selling volume"),
    FactorSpec("margin_short_balance_volume", "margin_short", "short balance volume"),
    FactorSpec("intraday_reversal_alpha", "intraday_microstructure", "first-hour reversal proxy"),
    FactorSpec("realized_vol_5m", "intraday_microstructure", "intraday realized volatility"),
    FactorSpec("amihud_5m", "intraday_microstructure", "intraday Amihud liquidity"),
    FactorSpec("close_vs_vwap", "intraday_microstructure", "close versus VWAP"),
    FactorSpec("range_position", "intraday_microstructure", "close position in intraday range"),
    FactorSpec("first_hour_volume_share", "intraday_microstructure", "first-hour volume share"),
    FactorSpec("last_hour_volume_share", "intraday_microstructure", "last-hour volume share"),
]

for rolling_window in ROLLING_WINDOWS:
    FACTOR_SPECS.extend(
        [
            FactorSpec(
                f"analyst_event_count_{rolling_window}d",
                "analyst_revision_rolling",
                f"{rolling_window}-day analyst event count",
            ),
            FactorSpec(
                f"analyst_revision_sum_{rolling_window}d",
                "analyst_revision_rolling",
                f"{rolling_window}-day analyst revision sum",
            ),
            FactorSpec(
                f"analyst_upgrade_minus_downgrade_{rolling_window}d",
                "analyst_revision_rolling",
                f"{rolling_window}-day upgrade minus downgrade",
            ),
            FactorSpec(
                f"analyst_rating_score_mean_{rolling_window}d",
                "analyst_revision_rolling",
                f"{rolling_window}-day rating score mean for covered names",
            ),
        ]
    )


def _safe_float(value: Any) -> float | None:
    if value is None or pd.isna(value) or not np.isfinite(value):
        return None
    return float(value)


def _feature_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or path.parent != Path("."):
        return path
    return ALT_FEATURES_DIR / path


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
    path = _feature_path(file_name)
    if not path.exists():
        return {"summary_file": file_name, "summary_missing": True}
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["summary_file"] = file_name
    return payload


def _output_path(base_name: str) -> Path:
    if not OUTPUT_SUFFIX:
        return RESULTS_DIR / base_name
    safe_suffix = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in OUTPUT_SUFFIX)
    stem, dot, ext = base_name.rpartition(".")
    if not dot:
        return RESULTS_DIR / f"{base_name}_{safe_suffix}"
    return RESULTS_DIR / f"{stem}_{safe_suffix}.{ext}"


def _load_base_alt_features(symbols: pd.Index) -> pd.DataFrame:
    path = _feature_path(STOCK_ALT_FEATURE_FILE)
    if not path.exists():
        raise RuntimeError(f"missing stock alternative features: {path}")
    df = pd.read_parquet(path)
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    universe = {str(symbol).zfill(6) for symbol in symbols}
    df = df[df["code"].isin(universe) & (df["date"] >= START_DATE)]
    return df.dropna(subset=["code", "date"]).sort_values(["date", "code"])


def _load_rolling_analyst_features(symbols: pd.Index, trading_index: pd.DatetimeIndex) -> pd.DataFrame:
    path = Path(ANALYST_RATINGS_FILE)
    if not path.exists():
        return pd.DataFrame()
    raw = pd.read_parquet(path)
    if raw.empty:
        return pd.DataFrame()
    universe = [str(symbol).zfill(6) for symbol in symbols]
    dates = trading_index[trading_index >= START_DATE]
    if len(dates) == 0:
        return pd.DataFrame()
    ratings = raw.copy()
    ratings["code"] = ratings["code"].astype(str).str.zfill(6)
    ratings["date"] = pd.to_datetime(ratings["publish_date"], errors="coerce").dt.normalize()
    ratings = ratings[ratings["code"].isin(set(universe))]
    ratings = ratings.dropna(subset=["code", "date"])
    if ratings.empty:
        return pd.DataFrame()
    ratings["event_count"] = 1.0
    ratings["revision_score"] = pd.to_numeric(ratings.get("revision_score"), errors="coerce").fillna(0.0)
    ratings["upgrade_minus_downgrade"] = (
        (ratings["revision_score"] > 0).astype(float)
        - (ratings["revision_score"] < 0).astype(float)
    )
    ratings["rating_score_sum"] = pd.to_numeric(ratings.get("rating_score"), errors="coerce").fillna(0.0)
    daily = ratings.groupby(["date", "code"], as_index=False).agg(
        event_count=("event_count", "sum"),
        revision_score=("revision_score", "sum"),
        upgrade_minus_downgrade=("upgrade_minus_downgrade", "sum"),
        rating_score_sum=("rating_score_sum", "sum"),
    )
    matrices = {
        column: daily.pivot_table(index="date", columns="code", values=column, aggfunc="sum")
        .reindex(index=dates, columns=universe)
        .fillna(0.0)
        for column in (
            "event_count",
            "revision_score",
            "upgrade_minus_downgrade",
            "rating_score_sum",
        )
    }
    frames: list[pd.DataFrame] = []
    for window in ROLLING_WINDOWS:
        event_count = matrices["event_count"].rolling(window, min_periods=1).sum()
        revision_sum = matrices["revision_score"].rolling(window, min_periods=1).sum()
        upgrade_minus_downgrade = matrices["upgrade_minus_downgrade"].rolling(
            window, min_periods=1
        ).sum()
        rating_score_sum = matrices["rating_score_sum"].rolling(window, min_periods=1).sum()
        rating_score_mean = rating_score_sum / event_count.replace(0, np.nan)
        frame = pd.concat(
            [
                event_count.stack().rename(f"analyst_event_count_{window}d"),
                revision_sum.stack().rename(f"analyst_revision_sum_{window}d"),
                upgrade_minus_downgrade.stack().rename(
                    f"analyst_upgrade_minus_downgrade_{window}d"
                ),
                rating_score_mean.stack().rename(f"analyst_rating_score_mean_{window}d"),
            ],
            axis=1,
        ).reset_index()
        frame = frame.rename(columns={"level_0": "date", "level_1": "code"})
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    result = frames[0]
    for frame in frames[1:]:
        result = result.merge(frame, on=["date", "code"], how="outer")
    result["date"] = pd.to_datetime(result["date"], errors="coerce").dt.normalize()
    result["code"] = result["code"].astype(str).str.zfill(6)
    return result


def _load_alt_features(symbols: pd.Index, trading_index: pd.DatetimeIndex) -> pd.DataFrame:
    base_features = _load_base_alt_features(symbols)
    rolling_features = _load_rolling_analyst_features(symbols, trading_index)
    if base_features.empty:
        return rolling_features
    if rolling_features.empty:
        return base_features
    return base_features.merge(rolling_features, on=["date", "code"], how="outer")


def _trading_position(index: pd.DatetimeIndex, signal_date: pd.Timestamp) -> int | None:
    eligible_date = signal_date + pd.Timedelta(days=SIGNAL_LAG_DAYS)
    pos = int(index.searchsorted(eligible_date, side="left"))
    if pos >= len(index):
        return None
    return pos


def _forward_return(close: pd.DataFrame, pos: int, horizon: int) -> pd.Series | None:
    end = pos + horizon
    if end >= len(close):
        return None
    current = close.iloc[pos].replace(0, np.nan)
    future = close.iloc[end]
    return (future / current - 1.0).replace([np.inf, -np.inf], np.nan)


def _rank_ic(values: pd.Series, forward: pd.Series) -> float | None:
    frame = pd.DataFrame({"x": values, "y": forward}).dropna()
    if len(frame) < MIN_CROSS_SECTION:
        return None
    if frame["x"].nunique() < 3 or frame["y"].nunique() < 3:
        return None
    return _safe_float(frame["x"].rank(pct=True).corr(frame["y"].rank(pct=True)))


def _bucket_return(values: pd.Series, forward: pd.Series) -> tuple[float | None, int]:
    frame = pd.DataFrame({"x": values, "y": forward}).dropna()
    if len(frame) < MIN_CROSS_SECTION:
        return None, len(frame)
    frame = frame.sort_values("x")
    bucket = max(1, int(len(frame) * TOP_BOTTOM_FRAC))
    if bucket * 2 > len(frame):
        return None, len(frame)
    bottom = float(frame.head(bucket)["y"].mean())
    top = float(frame.tail(bucket)["y"].mean())
    return _safe_float(top - bottom), len(frame)


def _summarize_series(values: list[float], dates: list[pd.Timestamp]) -> dict[str, Any]:
    if not values:
        return {
            "n_dates": 0,
            "mean": None,
            "std": None,
            "ir": None,
            "t_stat": None,
            "positive_rate": None,
            "train_mean": None,
            "test_mean": None,
        }
    series = pd.Series(values, index=pd.DatetimeIndex(dates), dtype=float).dropna()
    if series.empty:
        return {
            "n_dates": 0,
            "mean": None,
            "std": None,
            "ir": None,
            "t_stat": None,
            "positive_rate": None,
            "train_mean": None,
            "test_mean": None,
        }
    mean = float(series.mean())
    std = float(series.std(ddof=1)) if len(series) > 1 else np.nan
    ir = mean / std if np.isfinite(std) and std > 1e-12 else None
    t_stat = mean / (std / np.sqrt(len(series))) if np.isfinite(std) and std > 1e-12 else None
    train = series[series.index < SPLIT_DATE]
    test = series[series.index >= SPLIT_DATE]
    return {
        "n_dates": int(len(series)),
        "mean": round(mean, 6),
        "std": round(std, 6) if np.isfinite(std) else None,
        "ir": round(float(ir), 4) if ir is not None else None,
        "t_stat": round(float(t_stat), 4) if t_stat is not None else None,
        "positive_rate": round(float((series > 0).mean()), 4),
        "train_mean": round(float(train.mean()), 6) if len(train) else None,
        "test_mean": round(float(test.mean()), 6) if len(test) else None,
    }


def _same_direction(summary: dict[str, Any], direction: int) -> bool:
    """Require train/test segments to agree with the full-period signal direction."""
    for key in ("mean", "train_mean", "test_mean"):
        value = summary.get(key)
        if value is None or float(value) * direction <= 0:
            return False
    return True


def _evaluate_factor(
    close: pd.DataFrame,
    feature_rows: pd.DataFrame,
    spec: FactorSpec,
    horizon: int,
) -> dict[str, Any]:
    ic_values: list[float] = []
    ls_values: list[float] = []
    dates: list[pd.Timestamp] = []
    coverages: list[int] = []
    grouped = feature_rows[["date", "code", spec.name]].dropna(subset=[spec.name]).groupby("date")
    for feature_date, group in grouped:
        pos = _trading_position(close.index, pd.Timestamp(feature_date))
        if pos is None:
            continue
        forward = _forward_return(close, pos, horizon)
        if forward is None:
            continue
        values = pd.to_numeric(group.set_index("code")[spec.name], errors="coerce")
        aligned_forward = forward.reindex(values.index)
        ic = _rank_ic(values, aligned_forward)
        ls, coverage = _bucket_return(values, aligned_forward)
        if ic is None or ls is None:
            continue
        ic_values.append(ic)
        ls_values.append(ls)
        dates.append(pd.Timestamp(feature_date))
        coverages.append(coverage)
    ic_summary = _summarize_series(ic_values, dates)
    ls_summary = _summarize_series(ls_values, dates)
    direction = 1 if (ic_summary["mean"] or 0.0) >= 0 else -1
    oriented_ls = [value * direction for value in ls_values]
    oriented_ls_summary = _summarize_series(oriented_ls, dates)
    stability_pass = _same_direction(ic_summary, direction) and _same_direction(
        oriented_ls_summary, 1
    )
    qualified = bool(
        ic_summary["n_dates"] >= MIN_DATES
        and ic_summary["mean"] is not None
        and abs(float(ic_summary["mean"])) >= 0.01
        and ic_summary["t_stat"] is not None
        and abs(float(ic_summary["t_stat"])) >= 2.0
        and oriented_ls_summary["mean"] is not None
        and float(oriented_ls_summary["mean"]) > 0
        and stability_pass
    )
    return {
        "factor": spec.name,
        "category": spec.category,
        "description": spec.description,
        "horizon": horizon,
        "signal_lag_days": SIGNAL_LAG_DAYS,
        "recommended_direction": direction,
        "avg_coverage": round(float(np.mean(coverages)), 1) if coverages else 0.0,
        "rank_ic": ic_summary,
        "top_minus_bottom_return": ls_summary,
        "oriented_top_minus_bottom_return": oriented_ls_summary,
        "stability_pass": stability_pass,
        "qualified_for_research": qualified,
    }


def _category_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    frame = pd.DataFrame(
        [
            {
                "category": row["category"],
                "horizon": row["horizon"],
                "factor": row["factor"],
                "ic_mean": row["rank_ic"]["mean"],
                "abs_ic": abs(row["rank_ic"]["mean"]) if row["rank_ic"]["mean"] is not None else np.nan,
                "t_stat": row["rank_ic"]["t_stat"],
                "qualified": row["qualified_for_research"],
                "n_dates": row["rank_ic"]["n_dates"],
            }
            for row in rows
        ]
    )
    if frame.empty:
        return result
    for (category, horizon), group in frame.groupby(["category", "horizon"], sort=True):
        valid = group.dropna(subset=["abs_ic"])
        best = valid.sort_values("abs_ic", ascending=False).head(1)
        result.append(
            {
                "category": str(category),
                "horizon": int(horizon),
                "factors": int(len(group)),
                "qualified": int(group["qualified"].sum()),
                "best_factor": str(best.iloc[0]["factor"]) if not best.empty else None,
                "best_abs_ic": round(float(best.iloc[0]["abs_ic"]), 6) if not best.empty else None,
                "best_ic_mean": round(float(best.iloc[0]["ic_mean"]), 6) if not best.empty else None,
                "best_t_stat": (
                    round(float(best.iloc[0]["t_stat"]), 4)
                    if not best.empty and pd.notna(best.iloc[0]["t_stat"])
                    else None
                ),
            }
        )
    return result


def main() -> None:
    started = time.time()
    close, _volume, _amount, _turnover = base._load_aligned_data()
    features = _load_alt_features(close.columns, close.index)
    alt_feature_summary = _load_alt_feature_summary()
    available_specs = [spec for spec in FACTOR_SPECS if spec.name in features.columns]
    print(
        f"V18 IC diagnostics: features={len(features)} symbols={features['code'].nunique()} "
        f"dates={features['date'].nunique()} factors={len(available_specs)} horizons={HORIZONS}",
        flush=True,
    )
    rows: list[dict[str, Any]] = []
    for spec in available_specs:
        for horizon in HORIZONS:
            row = _evaluate_factor(close, features, spec, horizon)
            rows.append(row)
            ic_mean = row["rank_ic"]["mean"]
            print(
                f"{spec.name:36s} h={horizon:2d} n={row['rank_ic']['n_dates']:3d} "
                f"ic={ic_mean} t={row['rank_ic']['t_stat']} q={row['qualified_for_research']}",
                flush=True,
            )
    rows = sorted(
        rows,
        key=lambda row: (
            row["qualified_for_research"],
            abs(row["rank_ic"]["mean"] or 0.0),
            row["rank_ic"]["n_dates"],
        ),
        reverse=True,
    )
    flat = pd.DataFrame(
        [
            {
                "factor": row["factor"],
                "category": row["category"],
                "horizon": row["horizon"],
                "n_dates": row["rank_ic"]["n_dates"],
                "avg_coverage": row["avg_coverage"],
                "rank_ic_mean": row["rank_ic"]["mean"],
                "rank_ic_t": row["rank_ic"]["t_stat"],
                "rank_ic_positive_rate": row["rank_ic"]["positive_rate"],
                "train_ic_mean": row["rank_ic"]["train_mean"],
                "test_ic_mean": row["rank_ic"]["test_mean"],
                "recommended_direction": row["recommended_direction"],
                "oriented_ls_mean": row["oriented_top_minus_bottom_return"]["mean"],
                "oriented_ls_t": row["oriented_top_minus_bottom_return"]["t_stat"],
                "qualified_for_research": row["qualified_for_research"],
            }
            for row in rows
        ]
    )
    csv_path = _output_path("quant_alt_factor_ic_v18.csv")
    flat.to_csv(csv_path, index=False)
    report = {
        "ts": datetime.now().isoformat(),
        "version": "V18-alt-factor-ic-diagnostics",
        "research_only": True,
        "start_date": str(START_DATE.date()),
        "split_date": str(SPLIT_DATE.date()),
        "signal_lag_days": SIGNAL_LAG_DAYS,
        "min_cross_section": MIN_CROSS_SECTION,
        "min_dates": MIN_DATES,
        "horizons": HORIZONS,
        "stock_alt_feature_file": STOCK_ALT_FEATURE_FILE,
        "analyst_ratings_file": ANALYST_RATINGS_FILE,
        "alt_feature_summary": alt_feature_summary,
        "feature_rows": int(len(features)),
        "feature_symbols": int(features["code"].nunique()) if not features.empty else 0,
        "feature_dates": int(features["date"].nunique()) if not features.empty else 0,
        "qualified_count": int(sum(row["qualified_for_research"] for row in rows)),
        "category_summary": _category_summary(rows),
        "top_results": rows[:30],
        "csv": str(csv_path),
        "production_blockers": [
            "IC diagnostics are not a production strategy",
            "current order-flow/intraday sources still need daily archives before full historical PIT validation",
            "qualified factors require out-of-sample portfolio construction, WF and capacity tests before use",
        ],
        "elapsed_s": round(time.time() - started, 1),
    }
    out_path = _output_path("quant_alt_factor_ic_v18.json")
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {out_path}", flush=True)
    print(f"Wrote {csv_path}", flush=True)


if __name__ == "__main__":
    main()
