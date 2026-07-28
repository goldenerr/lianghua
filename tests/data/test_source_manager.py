"""Tests for DataSourceManager — provider routing and degradation."""

from unittest.mock import AsyncMock

import pandas as pd
import pytest
from quant_trading.config.market_router import MarketRouter
from quant_trading.config.settings import Market, MarketRules, SystemSettings, TradingSession
from quant_trading.data.provider import (
    DataProvider,
    DataProviderError,
    DataRequest,
    DataResult,
    Frequency,
)
from quant_trading.data.source_manager import DataSourceManager


class AlwaysFailProvider(DataProvider):
    def __init__(self, name: str = "fail") -> None:
        super().__init__(name)

    @property
    def supported_markets(self) -> list[str]:
        return ["A股"]

    async def fetch(self, request: DataRequest) -> DataResult:
        self.record_failure("boom")
        raise DataProviderError("boom", provider=self.name, symbol=request.symbol)


class AlwaysOkProvider(DataProvider):
    def __init__(self, name: str = "ok") -> None:
        super().__init__(name)

    @property
    def supported_markets(self) -> list[str]:
        return ["A股"]

    async def fetch(self, request: DataRequest) -> DataResult:
        data = pd.DataFrame(
            {"open": [1.0], "high": [1.1], "low": [0.9], "close": [1.0], "volume": [100.0]},
            index=pd.DatetimeIndex(pd.to_datetime(["2026-06-24"])),
        )
        self.record_success()
        return DataResult(
            symbol=request.symbol, frequency=request.frequency, data=data, source=self.name
        )


class TestDataSourceManager:
    def setup_method(self):
        MarketRouter._configure_for_testing(SystemSettings())

    def test_init_registers_defaults(self):
        mgr = DataSourceManager(data_dir="test_data/")
        providers = mgr.get_providers_for_market("A股")
        names = [p.name for p in providers]
        assert names[0] == "tencent_ifzq"
        assert "akshare" in names
        assert "yfinance" not in names
        assert mgr.get_missing_configured_sources("A股") == {"A股": ()}

    def test_crypto_market_providers(self):
        MarketRouter._configure_for_testing(
            SystemSettings(
                primary_markets=(Market.CRYPTO,),
                trading_sessions=(
                    TradingSession(
                        market=Market.CRYPTO,
                        sessions=(("00:00", "24:00"),),
                        timezone="UTC",
                    ),
                ),
                market_rules=(
                    MarketRules(
                        market=Market.CRYPTO,
                        tick_size=0.01,
                        lot_size=1,
                        price_precision=2,
                        data_sources=("ccxt", "binance"),
                        fee_model="crypto_maker_taker",
                    ),
                ),
            )
        )
        mgr = DataSourceManager(data_dir="test_data/")
        providers = mgr.get_providers_for_market("加密货币")
        names = [p.name for p in providers]
        assert any("ccxt" in n for n in names)

    def test_inactive_market_does_not_use_legacy_fallback(self):
        mgr = DataSourceManager(data_dir="test_data/")
        assert mgr.get_providers_for_market("加密货币") == []
        assert "not enabled" in mgr.get_missing_configured_sources("加密货币")["加密货币"][0]

    def test_unknown_market_returns_empty(self):
        mgr = DataSourceManager(data_dir="test_data/", use_configured_routing=False)
        providers = mgr.get_providers_for_market("火星")
        assert providers == []

    def test_custom_provider_registration(self):
        from quant_trading.data.yfinance_provider import YfinanceProvider

        mgr = DataSourceManager(data_dir="test_data/", use_configured_routing=False)
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

    @pytest.mark.asyncio
    async def test_fetch_tries_next_provider_in_same_call_after_provider_error(self):
        mgr = DataSourceManager(data_dir="test_data/", use_configured_routing=False)
        fail = AlwaysFailProvider("primary_fail")
        ok = AlwaysOkProvider("secondary_ok")
        mgr.register(fail, "A股", priority=0)
        mgr.register(ok, "A股", priority=1)
        mgr._market_priority["A股"] = ["primary_fail", "secondary_ok"]

        result = await mgr.fetch("600519", "A股")

        assert result.source == "secondary_ok"
        assert fail._fail_count == 1
        assert mgr.get_switch_history()[-1]["from"] == "primary_fail"
