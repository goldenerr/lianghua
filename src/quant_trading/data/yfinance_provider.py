"""
YFinance data provider — US stocks, HK stocks, global markets.

AGENTS.md §4: 数据交叉校验使用两个独立数据源。
"""

from __future__ import annotations

import logging

from .provider import (
    DataProvider,
    DataProviderError,
    DataRequest,
    DataResult,
    Frequency,
)

logger = logging.getLogger(__name__)


class YfinanceProvider(DataProvider):
    """Yahoo Finance data provider via yfinance library."""

    def __init__(self):
        super().__init__("yfinance")

    @property
    def supported_markets(self) -> list[str]:
        return ["A股", "美股", "港股", "期权"]

    def _to_yfinance_symbol(self, symbol: str) -> str:
        """Convert internal symbol to yfinance format."""
        # A-shares: 600519.SH → 600519.SS
        # or keep as-is if already formatted
        if symbol.endswith(".SH"):
            return symbol.replace(".SH", ".SS")
        if symbol.endswith(".SZ"):
            return symbol.replace(".SZ", ".SZ")  # yfinance uses .SZ for Shenzhen
        return symbol

    def _to_interval(self, frequency: Frequency) -> str:
        """Convert frequency to yfinance interval string."""
        mapping = {
            Frequency.DAILY: "1d",
            Frequency.HOURLY: "1h",
            Frequency.MINUTE: "1m",
        }
        return mapping.get(frequency, "1d")

    async def fetch(self, request: DataRequest) -> DataResult:
        """Fetch data from Yahoo Finance."""
        yf_symbol = self._to_yfinance_symbol(request.symbol)
        interval = self._to_interval(request.frequency)

        try:
            import yfinance as yf  # type: ignore[import-untyped]

            ticker = yf.Ticker(yf_symbol)

            # Build kwargs
            kwargs: dict = {"interval": interval}
            if request.start_date:
                kwargs["start"] = request.start_date.isoformat()
            if request.end_date:
                kwargs["end"] = request.end_date.isoformat()

            if request.frequency == Frequency.DAILY:
                hist = ticker.history(period="max", **kwargs)
            else:
                # For intraday, yfinance limits to 7-60 days
                period = "60d" if request.frequency == Frequency.MINUTE else "1mo"
                hist = ticker.history(period=period, **kwargs)

            if hist.empty:
                raise DataProviderError(
                    f"No data returned for {yf_symbol}",
                    provider=self.name,
                    symbol=request.symbol,
                )

            hist = self._normalize_columns(hist)
            self.record_success()

            return DataResult(
                symbol=request.symbol,
                frequency=request.frequency,
                data=hist,
                source=self.name,
            )

        except ImportError:
            raise DataProviderError(
                "yfinance not installed. Run: pip install yfinance",
                provider=self.name,
                symbol=request.symbol,
            )
        except DataProviderError:
            raise
        except Exception as e:
            self.record_failure(str(e))
            raise DataProviderError(
                str(e), provider=self.name, symbol=request.symbol
            ) from e
