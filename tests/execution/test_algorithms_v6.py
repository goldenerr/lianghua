import numpy as np
import pytest
from quant_trading.execution.algorithms import (
    MarketImpactParams,
    almgren_chriss,
    estimate_implementation_shortfall,
    smart_route_order,
    twap_schedule,
    vwap_schedule,
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
    buy = estimate_implementation_shortfall(1_000, 100.0, 1_000_000, 0.20, side="buy")
    sell = estimate_implementation_shortfall(1_000, 100.0, 1_000_000, 0.20, side="sell")
    assert sell["commission_bps"] > buy["commission_bps"]
    assert sell["total_cost_pct"] > buy["total_cost_pct"]

    small = smart_route_order(500, 100.0, 100_000, 0.20)
    medium = smart_route_order(2_000, 100.0, 100_000, 0.20)
    large = smart_route_order(8_000, 100.0, 100_000, 0.20)
    assert len(small) == 1
    assert len(medium) == 1
    assert len(large) == 3
    assert sum(int(schedule.shares_per_slot.sum()) for schedule in large) == 8_000
