"""Tests for DataSourceManager — provider routing and degradation."""

from unittest.mock import AsyncMock

import pandas as pd
import pytest
from quant_trading.data.provider import DataProviderError, DataResult, Frequency
from quant_trading.data.source_manager import DataSourceManager


class TestDataSourceManager:
    def test_init_registers_defaults(self):
        mgr = DataSourceManager(data_dir="test_data/")
        providers = mgr.get_providers_for_market("A股")
        names = [p.name for p in providers]
        assert "akshare" in names
        assert "yfinance" in names

    def test_crypto_market_providers(self):
        mgr = DataSourceManager(data_dir="test_data/")
        providers = mgr.get_providers_for_market("加密货币")
        names = [p.name for p in providers]
        assert any("ccxt" in n for n in names)

    def test_unknown_market_returns_empty(self):
        mgr = DataSourceManager(data_dir="test_data/")
        providers = mgr.get_providers_for_market("火星")
        assert providers == []

    def test_custom_provider_registration(self):
        from quant_trading.data.yfinance_provider import YfinanceProvider
        mgr = DataSourceManager(data_dir="test_data/")
        custom = YfinanceProvider()
        mgr.register(custom, "A股", priority=0)  # Highest priority
        providers = mgr.get_providers_for_market("A股")
        assert providers[0].name == "yfinance"

    def test_reset_failures(self):
        mgr = DataSourceManager(data_dir="test_data/")
        providers = mgr.get_providers_for_market("A股")
        # Artificially set failures
        providers[0]._fail_count = 5
        mgr.reset_failures()
        assert all(p._fail_count == 0 for p in mgr.get_providers_for_market("A股"))

    def test_switch_log(self):
        mgr = DataSourceManager(data_dir="test_data/")
        mgr._log_switch("akshare", "yfinance", "600519.SH", "timeout")
        history = mgr.get_switch_history()
        assert len(history) == 1
        assert history[0]["from"] == "akshare"
        assert history[0]["to"] == "yfinance"

    @pytest.mark.asyncio
    async def test_fetch_no_providers_for_market(self):
        mgr = DataSourceManager(data_dir="test_data/")
        with pytest.raises(DataProviderError, match="No providers"):
            await mgr.fetch("TEST", "火星")

    @pytest.mark.asyncio
    async def test_fetch_with_mock_provider(self):
        mgr = DataSourceManager(data_dir="test_data/")
        # Get the first A-share provider and mock its fetch
        providers = mgr.get_providers_for_market("A股")
        provider = providers[0]

        mock_df = pd.DataFrame(
            {"open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5], "volume": [1000.0]},
            index=pd.DatetimeIndex(pd.to_datetime(["2024-01-01"])),
        )
        mock_result = DataResult(
            symbol="600519.SH",
            frequency=Frequency.DAILY,
            data=mock_df,
            source=provider.name,
        )

        original_fetch = provider.fetch
        provider.fetch = AsyncMock(return_value=mock_result)

        try:
            result = await mgr.fetch("600519.SH", "A股")
            assert result.symbol == "600519.SH"
            assert result.source == provider.name
            assert not result.data.empty
        finally:
            provider.fetch = original_fetch

    @pytest.mark.asyncio
    async def test_fetch_skips_unhealthy_provider(self):
        mgr = DataSourceManager(data_dir="test_data/")
        providers = mgr.get_providers_for_market("A股")

        # Make first provider unhealthy
        providers[0]._fail_count = 5

        # Mock the second provider to succeed
        mock_df = pd.DataFrame(
            {"close": [10.0]}, index=pd.DatetimeIndex(pd.to_datetime(["2024-01-01"]))
        )
        mock_result = DataResult(
            symbol="TEST", frequency=Frequency.DAILY, data=mock_df, source=providers[1].name
        )
        original = providers[1].fetch
        providers[1].fetch = AsyncMock(return_value=mock_result)

        try:
            result = await mgr.fetch("TEST", "A股")
            # Should have used the second provider
            assert result.source == providers[1].name
        finally:
            providers[1].fetch = original
