"""Tests for DataQualityMonitor and AlertManager."""

import numpy as np
import pandas as pd
from quant_trading.data.quality_monitor import (
    Alert,
    AlertManager,
    DataQualityMonitor,
    SymbolHealth,
)


def _make_clean_df(n: int = 10) -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {
            "open": list(range(100, 100 + n)),
            "high": list(range(101, 101 + n)),
            "low": list(range(99, 99 + n)),
            "close": list(range(100, 100 + n)),
            "volume": [10000.0] * n,
        },
        index=pd.DatetimeIndex(pd.to_datetime(dates)),
    )


class TestAlertManager:
    def test_emit_alert(self):
        am = AlertManager()
        alert = Alert(symbol="TEST", severity="error", message="test alert")
        sent = am.emit(alert)
        assert sent is True

    def test_convergence_suppresses_duplicate(self):
        am = AlertManager()
        alert = Alert(symbol="TEST", severity="error", message="duplicate test")
        # First send
        assert am.emit(alert) is True
        # Second send within convergence window — should be suppressed
        assert am.emit(alert) is False

    def test_different_alerts_not_suppressed(self):
        am = AlertManager()
        a1 = Alert(symbol="A", severity="error", message="err1")
        a2 = Alert(symbol="B", severity="warning", message="warn1")
        assert am.emit(a1) is True
        assert am.emit(a2) is True

    def test_callback_invoked(self):
        am = AlertManager()
        received = []

        def cb(alert: Alert):
            received.append(alert.message)

        am.register_callback(cb)
        am.emit(Alert(symbol="X", severity="info", message="callback test"))
        assert len(received) == 1
        assert received[0] == "callback test"

    def test_callback_not_called_on_suppressed(self):
        am = AlertManager()
        received = []

        def cb(alert: Alert):
            received.append(alert.message)

        am.register_callback(cb)
        a = Alert(symbol="Y", severity="info", message="suppressed callback")
        am.emit(a)  # First — delivered
        am.emit(a)  # Second — suppressed
        assert len(received) == 1

    def test_recent_alerts(self):
        am = AlertManager()
        for i in range(5):
            am.emit(Alert(symbol=f"S{i}", severity="info", message=f"msg{i}"))
        recent = am.get_recent_alerts()
        assert len(recent) == 5


class TestDataQualityMonitor:
    def test_check_single_clean_data(self):
        monitor = DataQualityMonitor()
        df = _make_clean_df()
        health = monitor.check_single("TEST", df)
        assert health.score >= 90
        assert health.status == "healthy"

    def test_check_single_missing_data(self):
        monitor = DataQualityMonitor()
        df = _make_clean_df(n=100)
        df.loc[df.index[50], "close"] = np.nan
        df.loc[df.index[51], "close"] = np.nan  # 2/100 = 2% > 1% threshold
        health = monitor.check_single("BAD", df)
        assert health.score < 100

    def test_check_batch(self):
        monitor = DataQualityMonitor()
        data = {
            "A": _make_clean_df(),
            "B": _make_clean_df(),
        }
        results = monitor.check_batch(data)
        assert len(results) == 2
        assert results["A"].status == "healthy"
        assert results["B"].status == "healthy"

    def test_get_health_tracks(self):
        monitor = DataQualityMonitor()
        monitor.check_single("TRACKED", _make_clean_df())
        h = monitor.get_health("TRACKED")
        assert h is not None
        assert h.symbol == "TRACKED"

    def test_get_health_missing_returns_none(self):
        monitor = DataQualityMonitor()
        assert monitor.get_health("MISSING") is None

    def test_cross_validate_identical(self):
        monitor = DataQualityMonitor()
        df = _make_clean_df()
        result = monitor.cross_validate_pair("CROSS", df, df.copy(), "src_a", "src_b")
        assert result.passed is True

    def test_cross_validate_diff_detected(self):
        monitor = DataQualityMonitor()
        df1 = _make_clean_df()
        df2 = _make_clean_df()
        df2.loc[df2.index[5], "close"] = 200  # Big difference
        result = monitor.cross_validate_pair("DIFF", df1, df2, "src1", "src2")
        assert result.passed is False

    def test_dashboard_data(self):
        monitor = DataQualityMonitor()
        monitor.check_single("D", _make_clean_df())
        dashboard = monitor.get_dashboard_data()
        assert "symbols" in dashboard
        assert "recent_alerts" in dashboard
        assert len(dashboard["symbols"]) == 1

    def test_get_all_health(self):
        monitor = DataQualityMonitor()
        monitor.check_single("X", _make_clean_df())
        monitor.check_single("Y", _make_clean_df())
        all_health = monitor.get_all_health()
        assert set(all_health.keys()) == {"X", "Y"}


class TestSymbolHealth:
    def test_healthy(self):
        h = SymbolHealth(symbol="T", score=95)
        assert h.status == "healthy"

    def test_degraded(self):
        assert SymbolHealth(symbol="T", score=75).status == "degraded"

    def test_warning(self):
        assert SymbolHealth(symbol="T", score=55).status == "warning"

    def test_critical(self):
        assert SymbolHealth(symbol="T", score=30).status == "critical"
