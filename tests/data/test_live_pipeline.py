"""Tests for the in-memory portions of the live data pipeline."""

from quant_trading.data.live_pipeline import (
    DataScheduler,
    LiveDataFeed,
    LivePortfolioMonitor,
    Quote,
    RedisEventBus,
)


def test_feed_aggregates_and_emits_completed_bars():
    feed = LiveDataFeed(["AAPL"])
    emitted = []
    feed.on_bar.append(emitted.append)
    feed._aggregate_bar("AAPL", Quote("AAPL", 99, 101, price=100, volume=10, timestamp=60), "1m", 60)
    feed._aggregate_bar("AAPL", Quote("AAPL", 100, 102, price=101, volume=20, timestamp=120), "1m", 60)
    assert len(emitted) == 1
    assert emitted[0].open == 100
    assert emitted[0].close == 100
    assert emitted[0].volume == 10


def test_event_bus_publishes_to_subscribers_and_filters_events():
    bus = RedisEventBus(use_redis=False)
    received = []
    bus.subscribe("risk.alert", received.append)
    bus.publish_risk_alert("VAR", "limit breached", "CRITICAL")
    bus.publish_signal("alpha", "BTC/USDT", "entry", 0.2)
    assert received[0]["severity"] == "CRITICAL"
    assert len(bus.get_events(channel="risk.alert")) == 1
    assert len(bus.get_events()) == 2


def test_scheduler_records_success_and_failure(tmp_path):
    scheduler = DataScheduler(tmp_path)
    scheduler.add_job("ok", lambda: "done", schedule="daily")

    def fail():
        raise RuntimeError("source unavailable")

    scheduler.add_job("fail", fail, schedule="daily")
    scheduler.add_job("hourly", lambda: "skip", schedule="hourly")
    result = scheduler.run_daily_refresh()
    assert result["ok"]["status"] == "ok"
    assert result["fail"]["status"] == "error"
    assert "hourly" not in result
    assert scheduler.get_status()["n_jobs"] == 3


def test_live_monitor_marks_positions_and_calculates_drawdown():
    monitor = LivePortfolioMonitor(initial_capital=100_000)
    monitor.on_fill("AAPL", "buy", 100, 100)
    monitor.on_quote(Quote("AAPL", 89, 91, price=90))
    status = monitor.get_status()
    assert status["n_positions"] == 1
    assert status["drawdown"] < 0

    monitor.on_fill("AAPL", "sell", 100, 90)
    assert monitor.get_status()["n_positions"] == 0
