from quant_trading.risk.stop_loss import StopLossManager


def test_hard_and_trailing_stops_exit_positions_and_summarize() -> None:
    manager = StopLossManager(trail_stop_pct=0.10, hard_stop_pct=0.20)
    manager.add_positions({"HARD": 0.4, "TRAIL": 0.6}, {"HARD": 100.0, "TRAIL": 100.0}, day=0)
    manager.update_prices({"TRAIL": 120.0}, day=1)

    assert manager.update_prices({"HARD": 79.0, "TRAIL": 105.0}, day=2) == {"HARD", "TRAIL"}
    summary = manager.exit_summary()
    assert summary["total_exits"] == 2
    assert any(reason.startswith("hard_stop") for reason in summary["by_reason"])
    assert any(reason.startswith("trail_stop") for reason in summary["by_reason"])


def test_profit_time_and_rebalance_removal_paths() -> None:
    manager = StopLossManager(
        enable_trail=False,
        enable_hard=False,
        enable_profit=True,
        enable_time=True,
        profit_target_pct=0.25,
        max_hold_days=5,
    )
    manager.add_positions(
        {"WIN": 0.4, "STALE": 0.3, "REMOVE": 0.3}, {"WIN": 100, "STALE": 100, "REMOVE": 100}, 0
    )
    manager.remove_positions({"REMOVE"})
    assert "REMOVE" not in manager.current_positions()

    assert manager.update_prices({"WIN": 126, "STALE": 100}, day=6) == {"WIN", "STALE"}
    assert manager.exit_summary()["total_exits"] == 2
