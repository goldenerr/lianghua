"""
AKShare data provider — A-share market data (free, no API key required).

AGENTS.md §4: 数据交叉校验使用两个独立数据源 (e.g., yfinance + akshare).
"""

from __future__ import annotations

import logging
from datetime import timezone

import pandas as pd

from .provider import (
    DataProvider,
    DataProviderError,
    DataRequest,
    DataResult,
    Frequency,
)

logger = logging.getLogger(__name__)

UTC = timezone.utc


class AkshareProvider(DataProvider):
    """AKShare data provider for Chinese A-share market."""

    def __init__(self) -> None:
        super().__init__("akshare")

    @property
    def supported_markets(self) -> list[str]:
        return ["A股", "期货", "港股"]

    def _clean_symbol(self, symbol: str) -> str:
        """Convert 600519.SH → 600519 (akshare uses bare codes)."""
        return symbol.split(".")[0]

    def _parse_period(self, frequency: Frequency) -> str:
        """Convert frequency to akshare period string."""
        return {
            Frequency.DAILY: "daily",
            Frequency.HOURLY: "60",  # 60-minute
            Frequency.MINUTE: "5",
        }.get(frequency, "daily")

    def _to_utc_index(self, df: pd.DataFrame) -> pd.DataFrame:
        """Ensure DataFrame index is UTC tz-aware."""
        if isinstance(df.index, pd.DatetimeIndex):
            if df.index.tz is None:
                df.index = df.index.tz_localize("Asia/Shanghai").tz_convert("UTC")
            elif str(df.index.tz) != "UTC":
                df.index = df.index.tz_convert("UTC")
        return df

    async def fetch(self, request: DataRequest) -> DataResult:
        """Fetch A-share daily OHLCV data from AKShare."""
        code = self._clean_symbol(request.symbol)

        try:
            import akshare as ak

            if request.frequency == Frequency.DAILY:
                df = ak.stock_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=(
                        request.start_date.strftime("%Y%m%d") if request.start_date else "20000101"
                    ),
                    end_date=(
                        request.end_date.strftime("%Y%m%d") if request.end_date else "20500101"
                    ),
                    adjust="qfq",  # 前复权
                )

                if df is None or df.empty:
                    raise DataProviderError(
                        f"No data for {request.symbol}",
                        provider=self.name,
                        symbol=request.symbol,
                    )

                # Rename Chinese columns
                column_map = {
                    "日期": "date",
                    "开盘": "open",
                    "最高": "high",
                    "最低": "low",
                    "收盘": "close",
                    "成交量": "volume",
                    "成交额": "amount",
                    "换手率": "turnover_rate",
                }
                df = df.rename(columns={k: v for k, v in column_map.items() if k in df.columns})

                # Set date index and convert to UTC
                if "date" in df.columns:
                    df["date"] = pd.to_datetime(df["date"])
                    df = df.set_index("date")

                df = self._to_utc_index(df)
                df = self._normalize_columns(df)

                self.record_success()
                return DataResult(
                    symbol=request.symbol,
                    frequency=request.frequency,
                    data=df,
                    source=self.name,
                )

            else:
                raise DataProviderError(
                    f"Frequency {request.frequency} not yet supported",
                    provider=self.name,
                    symbol=request.symbol,
                )

        except ImportError as exc:
            raise DataProviderError(
                "akshare not installed. Run: pip install akshare",
                provider=self.name,
                symbol=request.symbol,
            ) from exc
        except DataProviderError:
            raise
        except Exception as e:
            self.record_failure(str(e))
            raise DataProviderError(str(e), provider=self.name, symbol=request.symbol) from e
