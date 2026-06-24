"""Tests for paper trading execution and risk safeguards."""

import pandas as pd
import pytest
from quant_trading.paper_trading import V59_CONFIG, PaperTradingEngine


def test_rebalance_buys_and_removes_position(tmp_path):
    engine = PaperTradingEngine(
        initial_capital=100_000,
        industry_data_path=tmp_path / "missing.parquet",
    )
    engine.execute_rebalance({"TEST": 0.20}, {"TEST": 100.0}, "2026-05-25")

    assert "TEST" in engine.account.positions
    assert engine.account.positions["TEST"].shares > 0
    assert engine.account.cash < engine.account.initial_capital
    assert engine.account.trade_log[-1]["side"] == "buy"

    engine.execute_rebalance({}, {"TEST": 101.0}, "2026-05-26")
    assert "TEST" not in engine.account.positions
    assert engine.account.trade_log[-1]["side"] == "sell"


def test_mdd_stop_keeps_recovery_floor_without_terminal_stop(tmp_path):
    engine = PaperTradingEngine(
        initial_capital=100_000,
        industry_data_path=tmp_path / "missing.parquet",
    )
    engine.account.cash = 75_000
    action = engine.check_mdd_safeguards()
    assert action.startswith("FLOOR")
    assert engine.stopped is False


def test_compute_positions_applies_mdd_stop_scale_floor(tmp_path):
    engine = PaperTradingEngine(
        initial_capital=100_000,
        industry_data_path=tmp_path / "missing.parquet",
    )
    engine.account.cash = 75_000
    snapshot = {}
    base_close = pd.Series(range(1, 260), dtype="float64").to_numpy()
    base_volume = pd.Series(range(1_000, 1_260), dtype="float64").to_numpy()
    for i in range(60):
        snapshot[f"S{i:03d}"] = {"close": base_close + i, "volume": base_volume + i}

    positions = engine.compute_positions(snapshot, current_date="2026-06-24")

    assert positions
    assert max(positions.values()) <= V59_CONFIG["mdd_stop_scale"] / len(positions) + 1e-12
    assert sum(positions.values()) == pytest.approx(V59_CONFIG["mdd_stop_scale"])


def test_status_and_report_use_marked_equity(tmp_path):
    engine = PaperTradingEngine(
        initial_capital=100_000,
        industry_data_path=tmp_path / "missing.parquet",
    )
    engine.execute_rebalance({"TEST": 0.20}, {"TEST": 100.0}, "2026-05-25")
    engine.account.update_market_values({"TEST": 110.0})
    status = engine.get_status()
    report = engine.generate_report()
    assert status["positions"] == 1
    assert status["total_equity"] > 0
    assert "total_return" in report


def test_paper_trading_uses_configured_fee_model_without_buy_stamp_duty(tmp_path):
    engine = PaperTradingEngine(
        initial_capital=100_000,
        industry_data_path=tmp_path / "missing.parquet",
    )

    engine.execute_rebalance({"TEST": 0.20}, {"TEST": 100.0}, "2026-05-25")
    buy = engine.account.trade_log[-1]
    expected_buy_slippage = 0.0005 * (1 + 0.10 * 0.20)
    expected_buy_price = 100.0 * (1 + expected_buy_slippage)
    expected_buy_cost = buy["shares"] * expected_buy_price * (1 + 0.00025)
    assert buy["cost"] == pytest.approx(round(expected_buy_cost, 2))

    engine.execute_rebalance({}, {"TEST": 101.0}, "2026-05-26")
    sell = engine.account.trade_log[-1]
    expected_sell_price = 101.0 * (1 - 0.0005)
    expected_sell_proceeds = sell["shares"] * expected_sell_price * (1 - 0.00075)
    assert sell["proceeds"] == pytest.approx(round(expected_sell_proceeds, 2))


def test_paper_trading_rejects_unapproved_fee_model(tmp_path):
    engine = PaperTradingEngine(
        initial_capital=100_000,
        config={**V59_CONFIG, "fee_model": "unapproved"},
        industry_data_path=tmp_path / "missing.parquet",
    )

    with pytest.raises(ValueError, match="unsupported execution fee model"):
        engine.execute_rebalance({"TEST": 0.20}, {"TEST": 100.0}, "2026-05-25")


def test_paper_trading_loads_industry_codes_with_or_without_exchange_suffix(tmp_path):
    industry_path = tmp_path / "industry.parquet"
    pd.DataFrame(
        [
            {"code": "000001", "industry": "bank"},
            {"code": "600000.SH", "industry": "broker"},
            {"code": "SZ000002", "industry": "property"},
            {"code": "bad", "industry": "ignored"},
        ]
    ).to_parquet(industry_path)

    engine = PaperTradingEngine(initial_capital=100_000, industry_data_path=industry_path)

    assert engine.industry_map["000001"] == "bank"
    assert engine.industry_map["600000"] == "broker"
    assert engine.industry_map["000002"] == "property"
    assert "bad" not in engine.industry_map
