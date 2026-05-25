"""
V7 Multi-Asset Trading Module — Crypto (via CCXT) + Futures support.
Extends the trading system beyond A-shares to global multi-asset.

Supported:
  - Crypto spot (Binance, OKX via CCXT)
  - Crypto perpetual futures
  - Chinese commodity futures (via akshare/sina)
  - Unified order/position/risk management across assets
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np
import pandas as pd

log = logging.getLogger("multi_asset")


# ═══════════════════════════════════════════════════════════════
# Asset Types & Market Definitions
# ═══════════════════════════════════════════════════════════════


class AssetClass(str, Enum):
    SPOT = "spot"
    FUTURES = "futures"
    PERPETUAL = "perpetual"
    OPTION = "option"


class Market(str, Enum):
    ASHARE = "ashare"
    CRYPTO = "crypto"
    COMMODITY = "commodity"
    FOREX = "forex"


@dataclass
class AssetSpec:
    """Specification for a tradable asset."""

    symbol: str
    exchange: str
    asset_class: AssetClass
    market: Market
    base_currency: str = "CNY"
    quote_currency: str = "CNY"
    tick_size: float = 0.01
    lot_size: int = 100
    min_notional: float = 100.0
    maker_fee: float = 0.0002  # 0.02%
    taker_fee: float = 0.0005  # 0.05%
    max_leverage: float = 1.0
    funding_rate_interval: int = 8  # hours (for perps)
    trading_hours: str = "24/7"
    is_active: bool = True


# ═══════════════════════════════════════════════════════════════
# Asset Registry
# ═══════════════════════════════════════════════════════════════

ASSET_REGISTRY: dict[str, AssetSpec] = {
    # Crypto spot (Binance)
    "BTC/USDT": AssetSpec(
        "BTC/USDT",
        "binance",
        AssetClass.SPOT,
        Market.CRYPTO,
        tick_size=0.01,
        lot_size=1,
        min_notional=10,
        maker_fee=0.001,
        taker_fee=0.001,
        max_leverage=1.0,
    ),
    "ETH/USDT": AssetSpec(
        "ETH/USDT",
        "binance",
        AssetClass.SPOT,
        Market.CRYPTO,
        tick_size=0.01,
        lot_size=1,
        min_notional=10,
        maker_fee=0.001,
        taker_fee=0.001,
        max_leverage=1.0,
    ),
    "SOL/USDT": AssetSpec(
        "SOL/USDT",
        "binance",
        AssetClass.SPOT,
        Market.CRYPTO,
        tick_size=0.01,
        lot_size=1,
        min_notional=10,
    ),
    # Crypto perpetual futures
    "BTC/USDT:USDT": AssetSpec(
        "BTC/USDT:USDT",
        "binance",
        AssetClass.PERPETUAL,
        Market.CRYPTO,
        tick_size=0.1,
        lot_size=1,
        min_notional=10,
        maker_fee=0.0002,
        taker_fee=0.0004,
        max_leverage=10.0,
        funding_rate_interval=8,
    ),
    "ETH/USDT:USDT": AssetSpec(
        "ETH/USDT:USDT",
        "binance",
        AssetClass.PERPETUAL,
        Market.CRYPTO,
        tick_size=0.01,
        lot_size=1,
        min_notional=10,
        maker_fee=0.0002,
        taker_fee=0.0004,
        max_leverage=10.0,
    ),
}

# A-share stocks pattern (instantiated dynamically)
ASHARE_SPEC = AssetSpec(
    "",
    "sse/szse",
    AssetClass.SPOT,
    Market.ASHARE,
    tick_size=0.01,
    lot_size=100,
    min_notional=100,
    maker_fee=0.00025,
    taker_fee=0.00025,
    max_leverage=1.0,
    trading_hours="9:30-11:30,13:00-15:00",
)


# ═══════════════════════════════════════════════════════════════
# Multi-Asset Data Provider
# ═══════════════════════════════════════════════════════════════


class MultiAssetDataProvider:
    """Unified data provider across spot, futures, and crypto."""

    def __init__(self) -> None:
        self._ccxt_exchanges: dict[str, Any] = {}

    def _get_ccxt(self, exchange_id: str = "binance") -> Any | None:
        if exchange_id not in self._ccxt_exchanges:
            try:
                import ccxt

                self._ccxt_exchanges[exchange_id] = getattr(ccxt, exchange_id)()
            except (ImportError, AttributeError):
                return None
        return self._ccxt_exchanges[exchange_id]

    def fetch_ohlcv(
        self,
        asset: AssetSpec,
        timeframe: str = "1d",
        limit: int = 500,
    ) -> np.ndarray | None:
        """Fetch OHLCV data for any asset type.

        Returns: numpy array [timestamp, open, high, low, close, volume]
        """
        if asset.market == Market.CRYPTO:
            return self._fetch_crypto_ohlcv(asset, timeframe, limit)
        elif asset.market == Market.ASHARE:
            return self._fetch_ashare_ohlcv(asset, limit)
        elif asset.market == Market.COMMODITY:
            return self._fetch_commodity_ohlcv(asset, limit)
        return None

    def _fetch_crypto_ohlcv(
        self, asset: AssetSpec, timeframe: str, limit: int
    ) -> np.ndarray | None:
        """Fetch crypto OHLCV via CCXT."""
        exchange = self._get_ccxt(asset.exchange)
        if exchange is None:
            return None

        try:
            ohlcv = exchange.fetch_ohlcv(asset.symbol, timeframe, limit=limit)
            return np.asarray(ohlcv, dtype=float)  # [ts, O, H, L, C, V]
        except Exception as e:
            log.warning(f"Crypto OHLCV {asset.symbol}: {e}")
            return None

    def _fetch_ashare_ohlcv(self, asset: AssetSpec, limit: int) -> np.ndarray | None:
        """Fetch A-share OHLCV via akshare."""
        try:
            import akshare as ak

            df = ak.stock_zh_a_hist(
                symbol=asset.symbol,
                period="daily",
                start_date="20200101",
                end_date="20500101",
                adjust="qfq",
            )
            if df is None or len(df) == 0:
                return None
            # Convert to numpy
            result = np.column_stack(
                [
                    df["日期"].astype(np.int64) // 10**9,
                    df["开盘"],
                    df["最高"],
                    df["最低"],
                    df["收盘"],
                    df["成交量"],
                ]
            )
            return np.asarray(result[-limit:], dtype=float)
        except Exception as e:
            log.warning(f"A-share OHLCV {asset.symbol}: {e}")
            return None

    def _fetch_commodity_ohlcv(self, asset: AssetSpec, limit: int) -> np.ndarray | None:
        """Fetch Chinese commodity futures via akshare."""
        try:
            import akshare as ak

            df = ak.futures_main_sina(symbol=asset.symbol)
            if df is None or len(df) == 0:
                return None
            return np.asarray(
                np.column_stack(
                    [
                        pd.to_datetime(df["日期"]).astype(np.int64) // 10**9,
                        df["开盘价"],
                        df["最高价"],
                        df["最低价"],
                        df["收盘价"],
                        df["成交量"],
                    ]
                )[-limit:],
                dtype=float,
            )
        except Exception:
            return None

    def fetch_funding_rate(self, asset: AssetSpec) -> float | None:
        """Fetch current funding rate for perpetual futures."""
        if asset.asset_class != AssetClass.PERPETUAL:
            return None

        exchange = self._get_ccxt(asset.exchange)
        if exchange is None:
            return None

        try:
            funding = exchange.fetch_funding_rate(asset.symbol)
            return float(funding.get("fundingRate", 0))
        except Exception:
            return None

    def fetch_order_book(self, asset: AssetSpec, depth: int = 10) -> dict[str, Any]:
        """Fetch order book for any asset."""
        exchange = self._get_ccxt(asset.exchange)
        if exchange is None:
            return {"bids": [], "asks": []}

        try:
            ob = exchange.fetch_order_book(asset.symbol, limit=depth)
            return {"bids": ob["bids"][:depth], "asks": ob["asks"][:depth]}
        except Exception:
            return {"bids": [], "asks": []}


# ═══════════════════════════════════════════════════════════════
# Cross-Asset Risk Management
# ═══════════════════════════════════════════════════════════════


@dataclass
class CrossAssetRiskConfig:
    """Risk limits across all asset classes."""

    # Per-asset-class limits
    max_exposure_pct: dict[AssetClass, float] = field(
        default_factory=lambda: {
            AssetClass.SPOT: 1.0,
            AssetClass.FUTURES: 0.3,
            AssetClass.PERPETUAL: 0.2,
        }
    )
    # Aggregate leverage across all derivatives
    max_total_leverage: float = 3.0
    # Currency exposure limits
    max_currency_exposure_pct: dict[str, float] = field(
        default_factory=lambda: {
            "USD": 0.5,
            "CNY": 1.0,
            "USDT": 0.3,
        }
    )
    # Per-asset limits
    max_single_asset_pct: float = 0.20
    # Correlation-based limits
    max_correlated_exposure_pct: float = 0.40
    # Funding rate limits (for perps)
    max_funding_rate_annual: float = 0.30  # 30% APR funding is too expensive


class CrossAssetRiskManager:
    """Multi-asset risk management."""

    def __init__(self, config: CrossAssetRiskConfig | None = None) -> None:
        self.config = config or CrossAssetRiskConfig()

    def check_exposure_limits(
        self,
        positions: dict[str, dict[str, float]],
        prices: dict[str, float],
        total_capital: float,
    ) -> dict[str, tuple[bool, str]]:
        """Check all cross-asset exposure limits.

        Returns: {check_name: (passed: bool, message: str)}
        """
        results: dict[str, tuple[bool, str]] = {}

        # Aggregate by asset class
        class_exposure: dict[AssetClass, float] = {}
        currency_exposure: dict[str, float] = {}
        total_leverage = 0.0

        for symbol, pos in positions.items():
            asset = ASSET_REGISTRY.get(symbol)
            if asset is None:
                continue

            mv = pos.get("market_value", 0)
            lev = pos.get("leverage", 1.0)

            class_exposure[asset.asset_class] = class_exposure.get(asset.asset_class, 0) + abs(mv)
            currency_exposure[asset.quote_currency] = currency_exposure.get(
                asset.quote_currency, 0
            ) + abs(mv)
            total_leverage += abs(mv) * lev

            # Single asset limit
            single_pct = abs(mv) / total_capital
            results[f"single_{symbol}"] = (
                single_pct <= self.config.max_single_asset_pct,
                f"{symbol}: {single_pct:.1%} / {self.config.max_single_asset_pct:.0%}",
            )

        # Asset class limits
        for aclass, limit in self.config.max_exposure_pct.items():
            exp = class_exposure.get(aclass, 0) / total_capital
            results[f"class_{aclass.value}"] = (
                exp <= limit,
                f"{aclass.value}: {exp:.1%} / {limit:.0%}",
            )

        # Total leverage
        leverage_ratio = total_leverage / total_capital
        results["total_leverage"] = (
            leverage_ratio <= self.config.max_total_leverage,
            f"Total leverage: {leverage_ratio:.1f}x / {self.config.max_total_leverage:.1f}x",
        )

        # Currency exposure
        for currency, limit in self.config.max_currency_exposure_pct.items():
            exp = currency_exposure.get(currency, 0) / total_capital
            results[f"currency_{currency}"] = (exp <= limit, f"{currency}: {exp:.1%} / {limit:.0%}")

        return results

    def should_reduce_exposure(
        self, check_results: dict[str, tuple[bool, str]]
    ) -> tuple[bool, list[str]]:
        """Determine if exposure should be reduced based on risk checks."""
        violations = [name for name, (passed, msg) in check_results.items() if not passed]
        return len(violations) > 0, violations


# ═══════════════════════════════════════════════════════════════
# Position & Order Manager (Multi-Asset)
# ═══════════════════════════════════════════════════════════════


class MultiAssetPositionManager:
    """Unified position tracking across assets."""

    def __init__(self) -> None:
        self.positions: dict[str, dict[str, Any]] = {}  # {symbol: {shares, avg_price, ...}}
        self.closed_pnl: float = 0.0
        self.funding_payments: float = 0.0

    def update_position(
        self, symbol: str, asset: AssetSpec, side: str, quantity: float, price: float
    ) -> None:
        """Update position after trade."""
        if symbol not in self.positions:
            self.positions[symbol] = {
                "shares": 0,
                "avg_price": 0,
                "realized_pnl": 0,
                "asset": asset,
                "side": "flat",
            }

        pos = self.positions[symbol]

        if side == "buy":
            new_shares = pos["shares"] + quantity
            if pos["shares"] <= 0 < new_shares:
                # Closing short → long
                closed_pnl = abs(pos["shares"]) * (pos["avg_price"] - price)
                pos["realized_pnl"] += closed_pnl
                self.closed_pnl += closed_pnl
                pos["shares"] = new_shares
                pos["avg_price"] = price
            elif pos["shares"] > 0:
                pos["avg_price"] = (
                    pos["shares"] * pos["avg_price"] + quantity * price
                ) / new_shares
                pos["shares"] = new_shares
            else:
                # Adding to short
                pos["shares"] = new_shares
                pos["avg_price"] = price
        else:  # sell
            new_shares = pos["shares"] - quantity
            if pos["shares"] >= 0 > new_shares:
                # Closing long → short
                closed_pnl = pos["shares"] * (price - pos["avg_price"])
                pos["realized_pnl"] += closed_pnl
                self.closed_pnl += closed_pnl
                pos["shares"] = new_shares
                pos["avg_price"] = price
            elif pos["shares"] < 0:
                pos["avg_price"] = (abs(pos["shares"]) * pos["avg_price"] + quantity * price) / abs(
                    new_shares
                )
                pos["shares"] = new_shares
            else:
                pos["shares"] = new_shares
                pos["avg_price"] = price

        pos["side"] = "long" if pos["shares"] > 0 else ("short" if pos["shares"] < 0 else "flat")

    def get_unrealized_pnl(self, symbol: str, current_price: float) -> float:
        """Calculate unrealized PnL for a position."""
        pos = self.positions.get(symbol)
        if pos is None or pos["side"] == "flat":
            return 0.0

        return float(pos["shares"] * (current_price - pos["avg_price"]))

    def get_total_equity(self, prices: dict[str, float], cash: float) -> float:
        """Calculate total equity across all positions."""
        total = cash + self.closed_pnl + self.funding_payments
        for symbol, pos in self.positions.items():
            if symbol in prices and pos["side"] != "flat":
                total += self.get_unrealized_pnl(symbol, prices[symbol])
        return total
