"""
Data Source Manager — priority-based routing with auto-degradation.

AGENTS.md §2 (data-001): 为每个市场配置数据源优先级列表，
主数据源连续失败 3 次或延迟超过阈值时自动切换到下一优先级数据源。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timezone

from quant_trading.config.market_calendar import CalendarRegistry
from quant_trading.config.market_router import MarketRouter
from quant_trading.config.settings import Market

from .akshare_provider import AkshareProvider
from .ccxt_provider import CcxtProvider
from .provider import (
    DataProvider,
    DataProviderError,
    DataRequest,
    DataResult,
    Frequency,
)
from .tencent_ifzq_provider import TencentIfzqProvider
from .yfinance_provider import YfinanceProvider

logger = logging.getLogger(__name__)
UTC = timezone.utc


# ── Provider registry ─────────────────────────────────────────────────────────


class DataSourceManager:
    """
    Manages data providers with priority-based routing and auto-degradation.

    AGENTS.md §2 (data-001):
      "主数据源连续失败3次或延迟超过阈值时，自动切换到下一优先级数据源"
      "记录切换日志（时间、原因、新数据源）并发出告警"
    """

    MAX_FAILURES = 3
    LATENCY_THRESHOLD_SECONDS = 30.0

    def __init__(
        self,
        data_dir: str = "data/",
        *,
        use_configured_routing: bool = True,
    ):
        self.data_dir = data_dir
        self.use_configured_routing = use_configured_routing
        self._providers: dict[str, DataProvider] = {}
        self._market_priority: dict[str, list[str]] = {}
        self._source_aliases: dict[str, tuple[str, ...]] = {}
        self._missing_configured_sources: dict[str, tuple[str, ...]] = {}
        self._switch_log: list[dict[str, str]] = []

        self._register_defaults()

    def _register_defaults(self) -> None:
        """Register all known providers and their market priorities."""
        # Yfinance
        yf = YfinanceProvider()
        self._providers[yf.name] = yf

        # AKShare
        ak = AkshareProvider()
        self._providers[ak.name] = ak

        # Tencent IFZQ direct HTTP — canonical A-share production source.
        tx = TencentIfzqProvider()
        self._providers[tx.name] = tx

        # CCXT (Binance, OKX)
        for ex in ["binance", "okx"]:
            ccxt_p = CcxtProvider(exchange_id=ex)
            self._providers[ccxt_p.name] = ccxt_p

        # Provider aliases translate config/system.yaml source identifiers into
        # concrete provider implementations without changing the approved order.
        self._source_aliases = {
            "tencent_ifzq": ("tencent_ifzq",),
            "tencent": ("tencent_ifzq",),
            "akshare": ("akshare",),
            "yfinance": ("yfinance",),
            "ccxt": ("ccxt/binance", "ccxt/okx"),
            "binance": ("ccxt/binance",),
            "okx": ("ccxt/okx",),
            # These identifiers are approved in config but require downstream
            # provider integrations before they can be used locally.
            "tushare": (),
            "polygon": (),
        }

        # Legacy fallback used only before the configuration router is installed.
        self._market_priority = {
            "A股": ["tencent_ifzq", "akshare"],
            "期货": ["akshare"],
            "加密货币": ["ccxt/binance", "ccxt/okx"],
            "美股": ["yfinance"],
            "港股": ["yfinance", "akshare"],
        }

    def register(self, provider: DataProvider, market: str, priority: int = 99) -> None:
        """Register a custom provider."""
        self._providers[provider.name] = provider
        self._source_aliases.setdefault(provider.name, (provider.name,))
        if market not in self._market_priority:
            self._market_priority[market] = []
        if priority >= len(self._market_priority[market]):
            self._market_priority[market].append(provider.name)
        else:
            self._market_priority[market].insert(priority, provider.name)

    def get_providers_for_market(self, market: str) -> list[DataProvider]:
        """Get ordered providers using the active market routing configuration."""
        names = self._configured_provider_names(market)
        if names is None:
            names = self._market_priority.get(market, [])
        providers = []
        for name in names:
            if name in self._providers:
                providers.append(self._providers[name])
        return providers

    def _configured_provider_names(self, market: str) -> list[str] | None:
        """Resolve provider names from the hash-bound MarketRouter snapshot.

        ``None`` means the router has not been installed yet and unit-level legacy
        fallback may be used. An empty list means the market is configured but is
        inactive or has no locally available provider, so fetch must fail closed.
        """
        if not self.use_configured_routing:
            return None
        if not CalendarRegistry.has_active_controls():
            return None
        try:
            market_enum = Market(market)
        except ValueError:
            return []
        try:
            configured_sources = MarketRouter.get_data_sources(market_enum)
        except ValueError as exc:
            self._missing_configured_sources[market] = (str(exc),)
            return []

        names: list[str] = []
        missing: list[str] = []
        for source in configured_sources:
            resolved = self._resolve_provider_alias(source)
            if not resolved:
                missing.append(source)
                continue
            for provider_name in resolved:
                if provider_name not in names:
                    names.append(provider_name)
        self._missing_configured_sources[market] = tuple(missing)
        return names

    def _resolve_provider_alias(self, source: str) -> tuple[str, ...]:
        candidates: Iterable[str] = self._source_aliases.get(source, (source,))
        return tuple(name for name in candidates if name in self._providers)

    def get_missing_configured_sources(
        self, market: str | None = None
    ) -> dict[str, tuple[str, ...]]:
        """Return configured source IDs that currently lack provider bindings."""
        if market is not None:
            # Resolve once so callers get a fresh diagnostic snapshot.
            self._configured_provider_names(market)
            return {market: self._missing_configured_sources.get(market, ())}
        return dict(self._missing_configured_sources)

    # ── Fetch with auto-degradation ───────────────────────────────────────

    async def fetch(
        self,
        symbol: str,
        market: str,
        frequency: Frequency = Frequency.DAILY,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> DataResult:
        """
        Fetch data with automatic provider degradation.

        Tries providers in priority order. On failure, records the failure,
        switches to next provider if consecutive failures exceed threshold.
        """
        providers = self.get_providers_for_market(market)
        if not providers:
            raise DataProviderError(
                f"No providers configured for market: {market}",
                provider="manager",
                symbol=symbol,
            )

        request = DataRequest(
            symbol=symbol,
            frequency=frequency,
            start_date=(datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None),
            end_date=(datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else None),
        )

        errors: list[str] = []
        for i, provider in enumerate(providers):
            if not provider.is_healthy:
                logger.warning(
                    "Skipping unhealthy provider %s for %s (failures=%d)",
                    provider.name,
                    symbol,
                    provider._fail_count,
                )
                errors.append(f"{provider.name}: unhealthy ({provider._last_error})")
                continue

            t0 = datetime.now(UTC)
            try:
                result = await provider.fetch(request)
                elapsed = (datetime.now(UTC) - t0).total_seconds()

                # Check latency
                if elapsed > self.LATENCY_THRESHOLD_SECONDS:
                    logger.warning(
                        "Provider %s latency %.1fs exceeds threshold %.1fs for %s",
                        provider.name,
                        elapsed,
                        self.LATENCY_THRESHOLD_SECONDS,
                        symbol,
                    )

                return result

            except DataProviderError as e:
                elapsed = (datetime.now(UTC) - t0).total_seconds()
                errors.append(f"{provider.name}: {e}")

                # Degrade inside the same fetch call so a single provider outage
                # cannot silently defer recovery until the next scheduler tick.
                if i + 1 < len(providers):
                    next_p = providers[i + 1].name
                    self._log_switch(provider.name, next_p, symbol, str(e))
                continue

        # All providers exhausted
        raise DataProviderError(
            f"All providers failed for {symbol} (market={market}): " + "; ".join(errors),
            provider="manager",
            symbol=symbol,
        )

    def _log_switch(self, from_provider: str, to_provider: str, symbol: str, reason: str) -> None:
        """Record provider switch event."""
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "from": from_provider,
            "to": to_provider,
            "symbol": symbol,
            "reason": reason,
        }
        self._switch_log.append(entry)
        logger.warning(
            "Data source switch: %s → %s for %s — reason: %s",
            from_provider,
            to_provider,
            symbol,
            reason,
        )
        # AGENTS.md §10: 告警收敛 — 相同事件 5 分钟内只发送一次
        # (告警通道集成在 monitor-001)

    def get_switch_history(self, limit: int = 50) -> list[dict[str, str]]:
        """Get recent provider switch history."""
        return self._switch_log[-limit:]

    def reset_failures(self) -> None:
        """Reset all providers' failure counters."""
        for p in self._providers.values():
            p.record_success()
