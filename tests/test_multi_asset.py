"""Tests for multi-asset market access, position tracking and risk limits."""

import numpy as np
from quant_trading.multi_asset import (
    ASSET_REGISTRY,
    AssetClass,
    CrossAssetRiskManager,
    MultiAssetDataProvider,
    MultiAssetPositionManager,
)


class FakeExchange:
    def fetch_ohlcv(self, symbol, timeframe, limit):
        return [[1, 100, 101, 99, 100.5, 10]][:limit]

    def fetch_funding_rate(self, symbol):
        return {"fundingRate": 0.0001}

    def fetch_order_book(self, symbol, limit):
        return {"bids": [[100, 1], [99, 2]], "asks": [[101, 1], [102, 2]]}


def test_crypto_provider_uses_exchange_adapter():
    provider = MultiAssetDataProvider()
    provider._ccxt_exchanges["binance"] = FakeExchange()
    spot = ASSET_REGISTRY["BTC/USDT"]
    perp = ASSET_REGISTRY["BTC/USDT:USDT"]

    data = provider.fetch_ohlcv(spot, limit=1)
    assert isinstance(data, np.ndarray)
    assert data.shape == (1, 6)
    assert provider.fetch_funding_rate(spot) is None
    assert provider.fetch_funding_rate(perp) == 0.0001
    assert len(provider.fetch_order_book(spot, depth=1)["bids"]) == 1


def test_cross_asset_risk_flags_single_asset_and_currency_exposure():
    manager = CrossAssetRiskManager()
    results = manager.check_exposure_limits(
        {"BTC/USDT:USDT": {"market_value": 40_000, "leverage": 3.0}},
        {},
        total_capital=100_000,
    )
    reduce, violations = manager.should_reduce_exposure(results)
    assert reduce is True
    assert "single_BTC/USDT:USDT" in violations
    assert "class_perpetual" in violations


def test_multi_asset_position_manager_tracks_realized_and_unrealized_pnl():
    asset = ASSET_REGISTRY["BTC/USDT"]
    pm = MultiAssetPositionManager()
    pm.update_position(asset.symbol, asset, "buy", 2, 100)
    assert pm.get_unrealized_pnl(asset.symbol, 110) == 20

    pm.update_position(asset.symbol, asset, "sell", 3, 120)
    assert pm.closed_pnl == 40
    assert pm.positions[asset.symbol]["side"] == "short"
    assert pm.get_total_equity({asset.symbol: 110}, cash=1_000) == 1_050


def test_registry_identifies_derivative_assets():
    assert ASSET_REGISTRY["BTC/USDT:USDT"].asset_class == AssetClass.PERPETUAL
