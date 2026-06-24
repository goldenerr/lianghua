"""Tests for Tencent IFZQ direct HTTP A-share provider."""

from datetime import date

import pandas as pd
import pytest

from quant_trading.data.provider import DataProviderError, DataRequest, Frequency
from quant_trading.data.tencent_ifzq_provider import TencentIfzqProvider


def test_tencent_provider_normalizes_symbols_with_exchange_prefix() -> None:
    provider = TencentIfzqProvider()

    assert provider._to_tencent_symbol("600519") == "sh600519"
    assert provider._to_tencent_symbol("600519.SH") == "sh600519"
    assert provider._to_tencent_symbol("000001.SZ") == "sz000001"
    assert provider._to_tencent_symbol("SZ000001") == "sz000001"


@pytest.mark.asyncio
async def test_tencent_provider_parses_qfq_kline_response(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = TencentIfzqProvider()
    payload = {
        "data": {
            "sh600519": {
                "qfqday": [
                    ["2026-06-22", "100.00", "101.00", "102.00", "99.00", "12345"],
                    ["2026-06-23", "101.00", "103.00", "104.00", "100.00", "23456"],
                ]
            }
        }
    }
    monkeypatch.setattr(provider, "_fetch_json", lambda _url: payload)

    result = await provider.fetch(
        DataRequest(
            symbol="600519.SH",
            frequency=Frequency.DAILY,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 23),
        )
    )

    assert result.source == "tencent_ifzq"
    assert result.symbol == "600519.SH"
    assert result.row_count == 2
    assert list(result.data.columns) == ["open", "close", "high", "low", "volume"]
    assert isinstance(result.data.index, pd.DatetimeIndex)
    assert str(result.data.index.tz) == "UTC"
    assert result.data.iloc[-1]["close"] == pytest.approx(103.0)


@pytest.mark.asyncio
async def test_tencent_provider_raises_provider_error_for_empty_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = TencentIfzqProvider()
    monkeypatch.setattr(provider, "_fetch_json", lambda _url: {"data": {"sh600519": {}}})

    with pytest.raises(DataProviderError, match="No kline data"):
        await provider.fetch(DataRequest(symbol="600519", frequency=Frequency.DAILY))

    assert provider._fail_count == 1
