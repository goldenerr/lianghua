"""
CCXT crypto data provider — Binance, OKX, etc.

AGENTS.md §2: 加密货币支持 24/7 交易。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

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


class CcxtProvider(DataProvider):
    """CCXT unified crypto exchange data provider."""

    def __init__(self, exchange_id: str = "binance"):
        super().__init__(f"ccxt/{exchange_id}")
        self.exchange_id = exchange_id

    @property
    def supported_markets(self) -> list[str]:
        return ["加密货币"]

    _FREQ_MAP: dict[Frequency, str] = {
        Frequency.DAILY: "1d",
        Frequency.HOURLY: "1h",
        Frequency.MINUTE: "1m",
    }

    def _to_ccxt_symbol(self, symbol: str) -> str:
        """Convert 'BTC/USDT' format. Already in CCXT format, keep as-is."""
        return symbol.upper()

    async def fetch(self, request: DataRequest) -> DataResult:
        """Fetch OHLCV data from CCXT exchange."""
        symbol = self._to_ccxt_symbol(request.symbol)
        timeframe = self._FREQ_MAP.get(request.frequency, "1d")

        try:
            import ccxt.async_support as ccxt_async  # type: ignore[import-untyped]

            exchange = getattr(ccxt_async, self.exchange_id)({
                "enableRateLimit": True,
                "timeout": 15000,
            })

            try:
                since_ms = None
                if request.start_date:
                    since_ms = int(
                        datetime.combine(
                            request.start_date, datetime.min.time(), tzinfo=UTC
                        ).timestamp() * 1000
                    )

                ohlcv = await exchange.fetch_ohlcv(
                    symbol, timeframe, since=since_ms, limit=request.limit
                )

                if not ohlcv:
                    raise DataProviderError(
                        f"No data for {symbol}",
                        provider=self.name,
                        symbol=request.symbol,
                    )

                df = pd.DataFrame(
                    ohlcv,
                    columns=["timestamp", "open", "high", "low", "close", "volume"],
                )
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                df = df.set_index("timestamp")
                df = self._normalize_columns(df)
                self.record_success()

                return DataResult(
                    symbol=request.symbol,
                    frequency=request.frequency,
                    data=df,
                    source=self.name,
                )

            finally:
                await exchange.close()

        except ImportError:
            raise DataProviderError(
                "ccxt not installed. Run: pip install ccxt",
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
