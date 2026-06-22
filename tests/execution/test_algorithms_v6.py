import numpy as np
import pytest
from quant_trading.config.market_router import MarketRouter
from quant_trading.config.settings import Market, MarketRules, SystemSettings, TradingSession
from quant_trading.execution.algorithms import (
    MarketImpactParams,
    almgren_chriss,
    estimate_implementation_shortfall,
    smart_route_order,
    twap_schedule,
    vwap_schedule,
)


def _configure_a_share_and_crypto(fee_model: str = "crypto_maker_taker") -> None:
    MarketRouter._configure_for_testing(
        SystemSettings(
            primary_markets=(Market.A_SHARES, Market.CRYPTO),
            trading_sessions=(
                TradingSession(
                    market=Market.A_SHARES,
                    sessions=(("09:30", "11:30"), ("13:00", "15:00")),
                    timezone="Asia/Shanghai",
                    holiday_calendar="XSHG",
                ),
                TradingSession(
                    market=Market.CRYPTO,
                    sessions=(("00:00", "24:00"),),
                    timezone="UTC",
                ),
            ),
            market_rules=(
                MarketRules(
                    market=Market.A_SHARES,
                    tick_size=0.01,
                    lot_size=100,
                    price_precision=2,
                    data_sources=("tushare",),
                    fee_model="cn_stock",
                    settlement_time="16:00",
                ),
                MarketRules(
                    market=Market.CRYPTO,
                    tick_size=0.01,
                    lot_size=1,
                    price_precision=2,
                    data_sources=("ccxt",),
                    fee_model=fee_model,
                ),
            ),
        )
    )


def test_twap_and_vwap_preserve_total_order_quantity() -> None:
    twap = twap_schedule(103, n_slots=10)
    vwap = vwap_schedule(103, np.array([1.0, 2.0, 7.0]), n_slots=3)

    assert int(twap.shares_per_slot.sum()) == 103
    assert int(vwap.shares_per_slot.sum()) == 103
    assert vwap.shares_per_slot[-1] > vwap.shares_per_slot[0]
    assert int(vwap_schedule(103, np.zeros(3), 3).shares_per_slot.sum()) == 103


def test_almgren_chriss_is_deterministic_and_enforces_capacity() -> None:
    first = almgren_chriss(1_000, 1_000_000, 0.20, 100.0, n_slots=5)
    second = almgren_chriss(1_000, 1_000_000, 0.20, 100.0, n_slots=5)

    np.testing.assert_array_equal(first.shares_per_slot, second.shares_per_slot)
    assert int(first.shares_per_slot.sum()) == 1_000
    assert first.expected_cost >= 0
    assert first.risk >= 0

    with pytest.raises(ValueError, match="capacity"):
        almgren_chriss(
            60_000,
            100_000,
            0.20,
            100.0,
            n_slots=10,
            params=MarketImpactParams(max_participation=0.05),
        )


def test_shortfall_accounts_for_sell_tax_and_router_conserves_quantity() -> None:
    _configure_a_share_and_crypto()
    buy = estimate_implementation_shortfall(
        1_000, 100.0, 1_000_000, 0.20, side="buy", market=Market.A_SHARES
    )
    sell = estimate_implementation_shortfall(
        1_000, 100.0, 1_000_000, 0.20, side="sell", market=Market.A_SHARES
    )
    assert sell["commission_bps"] > buy["commission_bps"]
    assert sell["total_cost_pct"] > buy["total_cost_pct"]

    small = smart_route_order(500, 100.0, 100_000, 0.20)
    medium = smart_route_order(2_000, 100.0, 100_000, 0.20)
    large = smart_route_order(8_000, 100.0, 100_000, 0.20)
    assert len(small) == 1
    assert len(medium) == 1
    assert len(large) == 3
    assert sum(int(schedule.shares_per_slot.sum()) for schedule in large) == 8_000


def test_shortfall_uses_configured_crypto_fee_model() -> None:
    _configure_a_share_and_crypto()
    maker = estimate_implementation_shortfall(
        1_000, 100.0, 1_000_000, 0.20, market=Market.CRYPTO, liquidity="maker"
    )
    taker = estimate_implementation_shortfall(
        1_000, 100.0, 1_000_000, 0.20, market=Market.CRYPTO, liquidity="taker"
    )

    assert maker["commission_bps"] == 2.0
    assert taker["commission_bps"] == 5.0
    assert taker["total_cost_pct"] > maker["total_cost_pct"]


def test_shortfall_rejects_unknown_configured_fee_model() -> None:
    _configure_a_share_and_crypto(fee_model="unapproved_fee_model")

    with pytest.raises(ValueError, match="unsupported execution fee model"):
        estimate_implementation_shortfall(1_000, 100.0, 1_000_000, 0.20, market=Market.CRYPTO)
