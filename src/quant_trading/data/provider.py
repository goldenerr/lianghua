"""
Unified data provider interface.

AGENTS.md §2 (data-001): 统一数据接口，根据 primary_markets 自动选择数据源。
AGENTS.md §4: 所有数据源必须满足质量门禁。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import cast

import pandas as pd


class Frequency(str, Enum):
    DAILY = "daily"
    MINUTE = "minute"
    TICK = "tick"
    HOURLY = "hourly"


@dataclass
class DataRequest:
    """Standardized data request."""

    symbol: str
    frequency: Frequency = Frequency.DAILY
    start_date: date | None = None
    end_date: date | None = None
    limit: int = 1000
    # Optional provider-specific params
    extra: dict[str, object] = field(default_factory=dict)


@dataclass
class DataResult:
    """Standardized data result with metadata."""

    symbol: str
    frequency: Frequency
    data: pd.DataFrame
    source: str  # Provider name
    fetched_at: datetime = field(default_factory=datetime.utcnow)
    data_range: tuple[date, date] | None = None  # (start, end) of actual data
    row_count: int = 0

    def __post_init__(self) -> None:
        self.row_count = len(self.data)
        if not self.data.empty and self.data_range is None:
            idx = self.data.index
            if isinstance(idx, pd.DatetimeIndex):
                self.data_range = (idx.min().date(), idx.max().date())

    @property
    def is_empty(self) -> bool:
        return bool(self.data.empty)


# Expected column schema after normalization
EXPECTED_COLUMNS = frozenset({"open", "high", "low", "close", "volume", "vwap", "trades"})


class DataProvider(ABC):
    """Abstract base for all data providers.

    All implementations must:
    1. Return DataResult with normalized column names (open/high/low/close/volume)
    2. Use UTC timezone for index
    3. Handle rate limiting gracefully
    4. Raise DataProviderError on failures
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._fail_count: int = 0
        self._last_error: str | None = None

    @property
    @abstractmethod
    def supported_markets(self) -> list[str]:
        """Markets this provider supports (e.g., ['A股', '加密货币'])."""

    @property
    def is_healthy(self) -> bool:
        """Provider considered healthy if < 3 consecutive failures."""
        return self._fail_count < 3

    def record_failure(self, error: str) -> None:
        self._fail_count += 1
        self._last_error = error

    def record_success(self) -> None:
        self._fail_count = 0
        self._last_error = None

    @abstractmethod
    async def fetch(self, request: DataRequest) -> DataResult:
        """Fetch data for a given request. Must be implemented by subclasses."""

    async def fetch_daily(
        self, symbol: str, start: date | None = None, end: date | None = None
    ) -> DataResult:
        """Convenience: fetch daily OHLCV data."""
        req = DataRequest(
            symbol=symbol,
            frequency=Frequency.DAILY,
            start_date=start,
            end_date=end,
        )
        return await self.fetch(req)

    def _normalize_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Normalize column names to standard OHLCV format.

        Maps common Chinese/English column names to standard ones.
        """
        column_map = {
            # Standard names
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
            "Vol": "volume",
            # Chinese names (akshare, tushare)
            "开盘": "open",
            "最高": "high",
            "最低": "low",
            "收盘": "close",
            "成交量": "volume",
            "成交额": "amount",
            # yfinance
            "Adj Close": "adj_close",
        }
        df = df.rename(columns={k: v for k, v in column_map.items() if k in df.columns})

        # Ensure index is datetime with UTC
        if not isinstance(df.index, pd.DatetimeIndex):
            for col in ["date", "trade_date", "datetime", "timestamp"]:
                if col in df.columns:
                    df["date"] = pd.to_datetime(df[col])
                    df = df.set_index("date")
                    break

        if isinstance(df.index, pd.DatetimeIndex) and df.index.tz is None:
            df.index = df.index.tz_localize("UTC")

        # Lowercase all column names
        df.columns = [c.lower() for c in df.columns]

        return cast(pd.DataFrame, df.sort_index())


class DataProviderError(Exception):
    """Raised when a data provider encounters a non-recoverable error."""

    def __init__(self, message: str, provider: str = "", symbol: str = ""):
        self.provider = provider
        self.symbol = symbol
        super().__init__(f"[{provider}] {symbol}: {message}")
