"""Tests for compliance, persistence, portfolio, and monitor modules."""
import numpy as np
import pytest
from datetime import date
from quant_trading.compliance.compliance import TaxCalculator, Jurisdiction, ComplianceReport as CompReport
from quant_trading.data.persistence import EventStore, StorageBackend
from quant_trading.portfolio.firewall import CapitalFirewall
from quant_trading.portfolio.optimizer import PortfolioOptimizer
from quant_trading.monitor.monitor import SystemMonitor
from quant_trading.monitor.dashboard import StrategyDashboard
from quant_trading.monitor.continuity import FaultInjector


# ── Compliance ──────────────────────────────────────────────────────

class TestTaxCalculator:
    def test_cn_stamp_duty(self):
        tc = TaxCalculator(Jurisdiction.CN)
        assert tc.stamp_duty(10000) == 5.0  # 0.05%

    def test_hk_stamp_duty(self):
        tc = TaxCalculator(Jurisdiction.HK)
        assert tc.stamp_duty(10000) == 10.0  # 0.1%

    def test_us_stamp_duty(self):
        tc = TaxCalculator(Jurisdiction.US)
        assert tc.stamp_duty(10000) == 0.0

    def test_cn_capital_gains(self):
        tc = TaxCalculator(Jurisdiction.CN)
        assert tc.capital_gains(1000) == 0.0  # China: no capital gains tax (for now)

    def test_us_capital_gains(self):
        tc = TaxCalculator(Jurisdiction.US)
        assert tc.capital_gains(1000) == pytest.approx(200.0)  # 20%

    def test_eu_capital_gains(self):
        tc = TaxCalculator(Jurisdiction.EU)
        assert tc.capital_gains(1000) == pytest.approx(250.0)  # 25%

    def test_loss_no_tax(self):
        tc = TaxCalculator(Jurisdiction.US)
        assert tc.capital_gains(-500) == 0.0


class TestComplianceReport:
    def test_create(self):
        r = CompReport(
            report_id="rpt-1", jurisdiction=Jurisdiction.CN,
            period_start=date(2026, 1, 1), period_end=date(2026, 1, 31),
            total_trades=50, total_volume=1_000_000, stamp_duty_paid=500,
        )
        assert r.report_id == "rpt-1"
        assert r.jurisdiction == Jurisdiction.CN

    def test_to_worm(self):
        r = CompReport(
            report_id="wrm-1", jurisdiction=Jurisdiction.US,
            period_start=date(2026, 1, 1), period_end=date(2026, 1, 1),
        )
        data = r.to_worm()
        assert isinstance(data, bytes)
        assert b"wrm-1" in data


# ── Persistence ─────────────────────────────────────────────────────

class TestEventStore:
    def test_append(self):
        es = EventStore()
        idx = es.append("order", {"symbol": "AAPL", "qty": 10})
        assert idx == 0
        assert len(es.events) == 1

    def test_append_multiple(self):
        es = EventStore()
        for i in range(5):
            es.append("event", {"i": i})
        assert len(es.events) == 5

    def test_append_returns_index(self):
        es = EventStore()
        es.append("e1", {})
        idx = es.append("e2", {})
        assert idx == 1

    def test_replay_all(self):
        es = EventStore()
        es.append("order", {"qty": 10}, tenant_id="t1")
        es.append("fill", {"qty": 10}, tenant_id="t1")
        es.append("order", {"qty": 20}, tenant_id="t2")
        events = es.replay()
        assert len(events) == 3

    def test_replay_by_tenant(self):
        es = EventStore()
        es.append("order", {"qty": 10}, tenant_id="t1")
        es.append("order", {"qty": 20}, tenant_id="t2")
        t1_events = es.replay(tenant_id="t1")
        assert len(t1_events) == 1

    def test_replay_sorted(self):
        es = EventStore()
        es.append("a", {"n": 1})
        es.append("b", {"n": 2})
        events = es.replay()
        assert events[0]["type"] == "a"
        assert events[1]["type"] == "b"

    def test_backend_default(self):
        es = EventStore()
        assert es.backend == StorageBackend.PARQUET


# ── Portfolio Firewall ──────────────────────────────────────────────

class TestCapitalFirewall:
    def test_allocate_within_limit(self):
        cf = CapitalFirewall(100000, max_per_strategy=0.3)
        assert cf.allocate("strat_a", 20000)
        assert "strat_a" in cf.allocations

    def test_allocate_exceeds_per_strategy(self):
        cf = CapitalFirewall(100000, max_per_strategy=0.3)
        assert not cf.allocate("strat_a", 40000)

    def test_allocate_exceeds_total(self):
        cf = CapitalFirewall(100000, max_per_strategy=1.0)
        cf.allocate("a", 60000)
        assert not cf.allocate("b", 50000)

    def test_release(self):
        cf = CapitalFirewall(100000)
        cf.allocate("a", 30000)
        released = cf.release("a")
        assert released == 30000
        assert "a" not in cf.allocations

    def test_release_nonexistent(self):
        cf = CapitalFirewall(100000)
        assert cf.release("ghost") == 0.0

    def test_available(self):
        cf = CapitalFirewall(100000)
        assert cf.available() == 100000
        cf.allocate("a", 30000)
        assert cf.available() == 70000

    def test_adjust_for_var(self):
        cf = CapitalFirewall(200000, max_per_strategy=1.0)
        cf.allocate("a", 100000)  # 50%
        cf.allocate("b", 50000)   # 25%
        adjustments = cf.adjust_for_var({"a": 0.3, "b": 0.2})
        # a: 100k > 60k (30% of 200k) → reduce by 40k
        assert "a" in adjustments
        assert adjustments["a"] == 40000

    def test_adjust_for_var_no_change(self):
        cf = CapitalFirewall(100000)
        cf.allocate("a", 20000)
        adjustments = cf.adjust_for_var({"a": 0.5})  # 50k limit > 20k
        assert len(adjustments) == 0


# ── Portfolio Optimizer ─────────────────────────────────────────────

class TestPortfolioOptimizer:
    def test_equal_weight(self):
        w = PortfolioOptimizer.equal_weight(5)
        assert len(w) == 5
        assert abs(w.sum() - 1.0) < 1e-10
        assert all(abs(x - 0.2) < 1e-10 for x in w)

    def test_risk_parity(self):
        rng = np.random.RandomState(42)
        returns = rng.normal(0.001, 0.02, (252, 3))
        cov = np.cov(returns.T)
        w = PortfolioOptimizer.risk_parity(cov)
        assert len(w) == 3
        assert abs(w.sum() - 1.0) < 1e-10

    def test_max_sharpe(self):
        rng = np.random.RandomState(42)
        returns = rng.normal(0.001, 0.02, (252, 3))
        w = PortfolioOptimizer.max_sharpe(returns)
        assert len(w) == 3
        assert abs(w.sum() - 1.0) < 1e-10

    def test_max_sharpe_singular(self):
        """Singular covariance should fall back to equal weight."""
        rng = np.random.RandomState(42)
        returns = np.tile(rng.normal(0.001, 0.02, 252), (2, 1)).T
        w = PortfolioOptimizer.max_sharpe(returns)
        assert len(w) == 2


# ── Monitor ─────────────────────────────────────────────────────────

class TestSystemMonitor:
    def test_defaults(self):
        sm = SystemMonitor()
        assert sm.get()["cpu_pct"] == 0

    def test_update(self):
        sm = SystemMonitor()
        sm.update(cpu_pct=50, mem_pct=30)
        assert sm.get()["cpu_pct"] == 50

    def test_healthy(self):
        sm = SystemMonitor()
        sm.update(cpu_pct=50, mem_pct=30)
        assert sm.is_healthy()

    def test_unhealthy_cpu(self):
        sm = SystemMonitor()
        sm.update(cpu_pct=85, mem_pct=30)
        assert not sm.is_healthy()

    def test_unhealthy_mem(self):
        sm = SystemMonitor()
        sm.update(cpu_pct=50, mem_pct=85)
        assert not sm.is_healthy()


class TestStrategyDashboard:
    def test_defaults(self):
        sd = StrategyDashboard(strategy_id="test_strat")
        assert sd.health_score == 100.0
        assert sd.active is True

    def test_intervene(self):
        sd = StrategyDashboard(strategy_id="s1")
        result = sd.intervene("pause", reason="high drawdown")
        assert result["action"] == "pause"
        assert result["requires_2fa"] is True
        assert result["confirmed"] is False

    def test_to_dict(self):
        sd = StrategyDashboard(strategy_id="s1", sharpe_20d=1.5, pnl_today=5000)
        d = sd.to_dict()
        assert d["strategy"] == "s1"
        assert d["sharpe"] == 1.5


class TestFaultInjector:
    def test_inject(self):
        fi = FaultInjector(target="order_gateway", fault_type="latency")
        result = fi.inject(duration_ms=200)
        assert result["injected"] is True
        assert result["duration_ms"] == 200

    def test_recover(self):
        fi = FaultInjector(target="data_feed")
        result = fi.recover()
        assert result["recovered"] is True
