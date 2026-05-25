"""Tests for paper trading execution and risk safeguards."""

from quant_trading.paper_trading import PaperTradingEngine


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


def test_mdd_stop_marks_paper_engine_stopped(tmp_path):
    engine = PaperTradingEngine(
        initial_capital=100_000,
        industry_data_path=tmp_path / "missing.parquet",
    )
    engine.account.cash = 75_000
    action = engine.check_mdd_safeguards()
    assert action.startswith("STOPPED")
    assert engine.stopped is True


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
