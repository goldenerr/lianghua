"""
Tencent IFZQ direct HTTP provider for A-share daily qfq OHLCV.

This intentionally avoids akshare wrappers because their Tencent endpoints have
known parser/rate-limit failures in this deployment.  The provider uses the
canonical direct endpoint documented in the project skill references.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timezone
from typing import Any, cast
from urllib.parse import quote
from urllib.request import Request, urlopen

import pandas as pd

from .provider import DataProvider, DataProviderError, DataRequest, DataResult, Frequency

logger = logging.getLogger(__name__)
UTC = timezone.utc


class TencentIfzqProvider(DataProvider):
    """A-share daily OHLCV provider backed by web.ifzq.gtimg.cn direct HTTP."""

    BASE_URL = "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    DEFAULT_START = date(2000, 1, 1)
    DEFAULT_END = date(2050, 1, 1)
    TIMEOUT_SECONDS = 10

    def __init__(self) -> None:
        super().__init__("tencent_ifzq")

    @property
    def supported_markets(self) -> list[str]:
        return ["A股"]

    def _to_tencent_symbol(self, symbol: str) -> str:
        """Normalize 600519 / 600519.SH / SZ000001 to sh600519 / sz000001."""
        normalized = symbol.strip().lower()
        if normalized.startswith(("sh", "sz")) and len(normalized) == 8:
            return normalized
        bare = normalized.split(".")[0]
        if len(bare) != 6 or not bare.isdigit():
            raise DataProviderError("invalid A-share symbol", provider=self.name, symbol=symbol)
        exchange = "sh" if bare.startswith(("5", "6", "9")) else "sz"
        return f"{exchange}{bare}"

    def _build_url(self, symbol: str, start: date, end: date, limit: int) -> str:
        param = f"{symbol},day,{start.isoformat()},{end.isoformat()},{limit},qfq"
        return f"{self.BASE_URL}?param={quote(param, safe=',')}"

    def _fetch_json(self, url: str) -> dict[str, Any]:
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 quant-trading/1.0"})
        with urlopen(request, timeout=self.TIMEOUT_SECONDS) as response:  # nosec B310 - fixed approved market-data endpoint
            raw = response.read().decode("utf-8")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("Tencent IFZQ response is not a JSON object")
        return parsed

    def _extract_rows(self, payload: dict[str, Any], symbol: str) -> list[list[str]]:
        data = payload.get("data")
        if not isinstance(data, dict):
            return []
        node = data.get(symbol)
        if not isinstance(node, dict):
            return []
        rows = node.get("qfqday") or node.get("day")
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, list) and len(row) >= 6]

    def _rows_to_frame(self, rows: list[list[str]]) -> pd.DataFrame:
        df = pd.DataFrame(
            rows,
            columns=["date", "open", "close", "high", "low", "volume", *range(6, len(rows[0]))],
        )
        df = df[["date", "open", "close", "high", "low", "volume"]]
        df["date"] = pd.to_datetime(df["date"])
        for col in ["open", "close", "high", "low", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["open", "close", "high", "low", "volume"])
        df = df.set_index("date").sort_index()
        if isinstance(df.index, pd.DatetimeIndex):
            df.index = df.index.tz_localize("Asia/Shanghai").tz_convert("UTC")
        return cast(pd.DataFrame, df)

    async def fetch(self, request: DataRequest) -> DataResult:
        if request.frequency != Frequency.DAILY:
            raise DataProviderError(
                f"Frequency {request.frequency} not supported by Tencent IFZQ direct provider",
                provider=self.name,
                symbol=request.symbol,
            )

        try:
            tencent_symbol = self._to_tencent_symbol(request.symbol)
            start = request.start_date or self.DEFAULT_START
            end = request.end_date or self.DEFAULT_END
            url = self._build_url(tencent_symbol, start, end, request.limit)
            payload = self._fetch_json(url)
            rows = self._extract_rows(payload, tencent_symbol)
            if not rows:
                self.record_failure("No kline data")
                raise DataProviderError("No kline data", provider=self.name, symbol=request.symbol)
            df = self._rows_to_frame(rows)
            if df.empty:
                self.record_failure("No valid OHLCV rows")
                raise DataProviderError(
                    "No valid OHLCV rows", provider=self.name, symbol=request.symbol
                )
            self.record_success()
            return DataResult(
                symbol=request.symbol,
                frequency=request.frequency,
                data=df,
                source=self.name,
            )
        except DataProviderError:
            raise
        except Exception as exc:
            self.record_failure(str(exc))
            raise DataProviderError(str(exc), provider=self.name, symbol=request.symbol) from exc
