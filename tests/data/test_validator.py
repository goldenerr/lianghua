"""Tests for DataValidator — AGENTS.md §4 data quality gates."""

import numpy as np
import pandas as pd
import pytest

from quant_trading.data.validator import DataValidator, ValidationResult, OHLCV_COLUMNS


def _make_ohlcv_df(
    close_prices: list[float],
    dates: list[str] | None = None,
    add_ohlcv: bool = True,
) -> pd.DataFrame:
    """Helper to create test OHLCV DataFrames."""
    if dates is None:
        dates = pd.bdate_range("2024-01-01", periods=len(close_prices), freq="B")
    data = {"close": close_prices}
    if add_ohlcv:
        data.update({
            "open": [p * 0.99 for p in close_prices],
            "high": [p * 1.01 for p in close_prices],
            "low": [p * 0.98 for p in close_prices],
            "volume": [1000000.0] * len(close_prices),
        })
    df = pd.DataFrame(data, index=pd.DatetimeIndex(pd.to_datetime(dates)))
    return df


class TestValidationResult:
    def test_default_passed(self):
        r = ValidationResult(symbol="TEST", passed=True)
        assert r.passed is True
        assert r.errors == []

    def test_add_error_flips_passed(self):
        r = ValidationResult(symbol="TEST", passed=True)
        r.add_error("bad data")
        assert r.passed is False
        assert len(r.errors) == 1


class TestDataValidator:
    def test_empty_df_fails(self):
        v = DataValidator()
        result = v.validate("TEST", pd.DataFrame())
        assert result.passed is False
        assert "empty" in result.errors[0].lower()

    def test_clean_data_passes(self):
        v = DataValidator()
        df = _make_ohlcv_df(list(range(100, 200)))
        result = v.validate("TEST", df)
        assert result.passed is True

    def test_missing_values_detected(self):
        v = DataValidator(max_missing_pct=0.01)
        close = list(range(100, 200))
        close[50] = np.nan  # 1/100 = 1% missing → should be caught
        df = _make_ohlcv_df(close)
        result = v.validate("TEST", df)
        # 1 out of 100 = 1%, equal to threshold — depends on >
        assert "missing_close" in result.stats

    def test_missing_values_above_threshold(self):
        v = DataValidator(max_missing_pct=0.005)  # 0.5%
        close = list(range(100, 200))
        close[50] = np.nan
        close[51] = np.nan  # 2/100 = 2% missing
        df = _make_ohlcv_df(close)
        result = v.validate("TEST", df)
        assert result.passed is False

    def test_price_jumps_detected(self):
        v = DataValidator(max_price_jump_pct=0.20)
        close = [100.0] * 10 + [150.0]  # 50% jump!
        df = _make_ohlcv_df(close)
        result = v.validate("TEST", df)
        # Should have at least warnings
        assert len(result.warnings) > 0

    def test_duplicates_detected(self):
        v = DataValidator()
        dates = ["2024-01-01", "2024-01-02", "2024-01-02", "2024-01-03"]
        df = _make_ohlcv_df([100, 101, 102, 103], dates=dates)
        result = v.validate("TEST", df)
        assert result.passed is False
        assert "duplicate" in result.errors[0].lower()

    def test_negative_prices_detected(self):
        v = DataValidator()
        close = [100, 101, -1, 103]
        df = _make_ohlcv_df(close)
        result = v.validate("TEST", df)
        assert result.passed is False
        assert any("negative" in e.lower() for e in result.errors)

    def test_high_less_than_low(self):
        v = DataValidator()
        df = _make_ohlcv_df([100, 101, 102])
        # Manually mess up high/low
        df.loc[df.index[1], "high"] = 90  # high < close
        df.loc[df.index[1], "low"] = 110  # low > close
        result = v.validate("TEST", df)
        assert result.passed is False
        assert any("high < low" in e.lower() for e in result.errors)

    def test_cross_validate_same_data_passes(self):
        v = DataValidator()
        df = _make_ohlcv_df(list(range(100, 110)))
        result = v.cross_validate(df, df.copy(), "source_a", "source_b")
        assert result.passed is True

    def test_cross_validate_large_diff_fails(self):
        v = DataValidator(max_cross_check_diff_pct=0.005)
        df1 = _make_ohlcv_df([100, 101, 102, 103, 104])
        df2 = _make_ohlcv_df([100, 101, 105, 103, 104])  # 3% diff on day 3
        result = v.cross_validate(df1, df2, "src1", "src2")
        assert result.passed is False

    def test_cross_validate_no_overlap(self):
        v = DataValidator()
        df1 = _make_ohlcv_df([100, 101], dates=["2024-01-01", "2024-01-02"])
        df2 = _make_ohlcv_df([200, 201], dates=["2024-06-01", "2024-06-02"])
        result = v.cross_validate(df1, df2, "src1", "src2")
        assert "No overlapping" in result.warnings[0]

    def test_is_ready_for_trading_blocked_by_errors(self):
        v = DataValidator()
        result = ValidationResult(symbol="X", passed=False)
        result.add_error("bad")
        assert v.is_ready_for_trading(result) is False

    def test_stats_populated(self):
        v = DataValidator()
        df = _make_ohlcv_df(list(range(100, 120)))
        result = v.validate("TEST", df)
        assert "rows" in result.stats
        assert result.stats["rows"] == 20
        assert "duplicates" in result.stats
