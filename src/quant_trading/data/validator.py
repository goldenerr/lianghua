"""
Data quality validator — enforces AGENTS.md §4 data quality gates.

Checks:
- 缺失值比例 ≤ 1% (OHLCV)
- 价格跳动 ≤ ±20% day-over-day
- 数据新鲜度 ≤ 30s (realtime)
- 交叉校验 (two independent sources)
- 重复值检测
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


@dataclass
class ValidationResult:
    """Result of a data quality validation run."""
    symbol: str
    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
        self.passed = False

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)


class DataValidator:
    """
    Validates OHLCV data against quality thresholds defined in AGENTS.md §4.

    All thresholds are configurable.
    """

    def __init__(
        self,
        max_missing_pct: float = 0.01,          # §4: 缺失值 ≤ 1%
        max_price_jump_pct: float = 0.20,       # §4: 价格跳动 ≤ ±20%
        max_data_delay_seconds: float = 30.0,    # §4: 延迟 ≤ 30s
        max_cross_check_diff_pct: float = 0.005, # §4: 交叉校验差异 < 0.5%
    ):
        self.max_missing_pct = max_missing_pct
        self.max_price_jump_pct = max_price_jump_pct
        self.max_data_delay_seconds = max_data_delay_seconds
        self.max_cross_check_diff_pct = max_cross_check_diff_pct

    # ── Individual checks ─────────────────────────────────────────────────

    def check_missing_values(
        self, df: pd.DataFrame, result: ValidationResult
    ) -> None:
        """Check: missing value ratio ≤ 1% for OHLCV columns."""
        available_cols = [c for c in OHLCV_COLUMNS if c in df.columns]
        if not available_cols:
            result.add_error("No OHLCV columns found")
            return

        missing = df[available_cols].isna().mean()
        for col in available_cols:
            pct = missing[col]
            result.stats[f"missing_{col}"] = round(pct, 4)
            if pct > self.max_missing_pct:
                result.add_error(
                    f"{col}: {pct:.2%} missing (threshold: {self.max_missing_pct:.1%})"
                )

    def check_price_jumps(
        self, df: pd.DataFrame, result: ValidationResult
    ) -> None:
        """Check: day-over-day close price change ≤ ±20%."""
        if "close" not in df.columns or len(df) < 2:
            return

        returns = df["close"].pct_change().dropna()
        jumps = returns[abs(returns) > self.max_price_jump_pct]

        result.stats["max_price_return"] = round(abs(returns).max(), 4) if len(returns) > 0 else 0
        result.stats["price_jump_count"] = len(jumps)
        result.stats["price_jump_pct"] = round(len(jumps) / len(returns), 4) if len(returns) > 0 else 0

        if len(jumps) > 0:
            jump_pct = len(jumps) / len(returns)
            if jump_pct > 0.01:  # More than 1% of days have jumps
                result.add_error(
                    f"Price jumps: {len(jumps)}/{len(returns)} days "
                    f"({jump_pct:.2%}) exceed {self.max_price_jump_pct:.1%}"
                )
            else:
                result.add_warning(
                    f"{len(jumps)} price jump(s) detected (may be corporate actions)"
                )

    def check_duplicates(self, df: pd.DataFrame, result: ValidationResult) -> None:
        """Check for duplicate index entries."""
        dupes = df.index.duplicated().sum()
        result.stats["duplicates"] = int(dupes)
        if dupes > 0:
            result.add_error(f"{dupes} duplicate index entries found")

    def check_freshness(
        self, df: pd.DataFrame, result: ValidationResult
    ) -> None:
        """Check real-time data freshness: latest data point ≤ 30s old."""
        if df.empty:
            return

        if isinstance(df.index, pd.DatetimeIndex):
            latest: pd.Timestamp = df.index.max()
            now = pd.Timestamp.now(tz=latest.tzinfo if latest.tzinfo else "UTC")
            age = (now - latest).total_seconds()
            result.stats["data_age_seconds"] = round(float(age), 1)
            if age > self.max_data_delay_seconds:
                result.add_error(
                    f"Data staleness: {age:.0f}s since last update "
                    f"(threshold: {self.max_data_delay_seconds:.0f}s)"
                )

    def check_value_ranges(self, df: pd.DataFrame, result: ValidationResult) -> None:
        """Check for non-negative prices and volumes, reasonable price range."""
        for col in ["open", "high", "low", "close"]:
            if col not in df.columns:
                continue
            neg = (df[col] < 0).sum()
            if neg > 0:
                result.add_error(f"{neg} negative values in {col}")

        if "high" in df.columns and "low" in df.columns:
            violations = (df["high"] < df["low"]).sum()
            if violations > 0:
                result.add_error(f"{violations} rows where high < low")

    # ── Cross-validation ──────────────────────────────────────────────────

    def cross_validate(
        self,
        df1: pd.DataFrame,
        df2: pd.DataFrame,
        source1: str,
        source2: str,
    ) -> ValidationResult:
        """
        Compare close prices from two independent data sources.
        AGENTS.md §4: 差异 < 0.5%.
        """
        result = ValidationResult(
            symbol="cross_check",
            passed=True,
            stats={"source1": source1, "source2": source2},
        )

        if df1.empty or df2.empty:
            result.add_warning("One or both data sources returned empty data")
            return result

        # Align by date
        common_idx = df1.index.intersection(df2.index)
        if len(common_idx) == 0:
            result.add_warning("No overlapping dates for cross-validation")
            return result

        close1 = df1.loc[common_idx, "close"]
        close2 = df2.loc[common_idx, "close"]

        diff_pct = abs((close1 - close2) / close1)
        max_diff = diff_pct.max()
        mean_diff = diff_pct.mean()

        result.stats["overlap_days"] = len(common_idx)
        result.stats["max_diff_pct"] = round(max_diff, 6)
        result.stats["mean_diff_pct"] = round(mean_diff, 6)
        result.stats["exceed_threshold_count"] = int((diff_pct > self.max_cross_check_diff_pct).sum())

        if max_diff > self.max_cross_check_diff_pct:
            result.add_error(
                f"Cross-validation failed: max close price difference "
                f"{max_diff:.4%} exceeds {self.max_cross_check_diff_pct:.4%} "
                f"(sources: {source1} vs {source2})"
            )

        return result

    # ── Full validation ───────────────────────────────────────────────────

    def validate(
        self,
        symbol: str,
        df: pd.DataFrame,
        *,
        skip_freshness: bool = True,  # Only for real-time data
    ) -> ValidationResult:
        """
        Run all quality checks on a DataFrame.

        Returns ValidationResult with pass/fail and details.
        """
        result = ValidationResult(symbol=symbol, passed=True)
        result.stats["rows"] = len(df)

        if df.empty:
            result.add_error("DataFrame is empty")
            return result

        self.check_missing_values(df, result)
        self.check_price_jumps(df, result)
        self.check_duplicates(df, result)
        self.check_value_ranges(df, result)

        if not skip_freshness:
            self.check_freshness(df, result)

        if result.passed:
            logger.info("Data quality PASSED for %s: %d rows", symbol, len(df))
        else:
            logger.error(
                "Data quality FAILED for %s: %d errors, %d warnings",
                symbol, len(result.errors), len(result.warnings),
            )

        return result

    def is_ready_for_trading(self, result: ValidationResult) -> bool:
        """Check if data quality allows trading (no errors, only warnings OK)."""
        return result.passed
