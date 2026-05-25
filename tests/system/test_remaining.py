"""Tests for remaining 0% coverage modules — risk advanced, ops, backtest, factor, impact."""
import numpy as np
from quant_trading.backtest.optimized import vectorized_mdd, vectorized_sharpe, vectorized_trade_pnl
from quant_trading.factor_store import FactorDefinition, FactorStore
from quant_trading.impact_assessment import ImpactAssessment
from quant_trading.operations import BackupManager
from quant_trading.operations_mr import FailoverManager, Region
from quant_trading.risk.advanced import CrossMarketMargin, GreeksCalculator, KillSwitch

# ── Risk Advanced ───────────────────────────────────────────────────

class TestGreeksCalculator:
    def test_delta(self):
        greeks = GreeksCalculator.delta(S=100, K=100, T=0.5, r=0.03, sigma=0.2)
        assert "delta" in greeks
        assert "gamma" in greeks
        assert "vega" in greeks
        assert "theta" in greeks

    def test_call_vs_put(self):
        call = GreeksCalculator.delta(100, 100, 0.5, 0.03, 0.2, is_call=True)
        put = GreeksCalculator.delta(100, 100, 0.5, 0.03, 0.2, is_call=False)
        assert call["delta"] > 0
        assert put["delta"] < 0


class TestCrossMarketMargin:
    def test_add_account(self):
        cmm = CrossMarketMargin()
        cmm.add_account("binance", 10000, 2000)
        assert cmm.total_exposure() == 2000

    def test_total_exposure(self):
        cmm = CrossMarketMargin()
        cmm.add_account("a", 50000, 10000)
        cmm.add_account("b", 30000, 5000)
        assert cmm.total_exposure() == 15000

    def test_available_margin(self):
        cmm = CrossMarketMargin()
        cmm.add_account("a", 50000, 10000)
        cmm.add_account("b", 30000, 5000)
        assert cmm.available_margin() == 65000  # (50000-10000)+(30000-5000)

    def test_empty(self):
        cmm = CrossMarketMargin()
        assert cmm.total_exposure() == 0
        assert cmm.available_margin() == 0


class TestKillSwitch:
    def test_default_inactive(self):
        ks = KillSwitch()
        assert not ks.is_active

    def test_activate(self):
        ks = KillSwitch()
        ks.activate("daily loss limit breached")
        assert ks.is_active

    def test_deactivate(self):
        ks = KillSwitch()
        ks.activate("test")
        ks.deactivate()
        assert not ks.is_active

    def test_activate_with_reason(self):
        ks = KillSwitch()
        ks.activate("circuit breaker tripped")
        assert ks.is_active
        assert ks.reason == "circuit breaker tripped"
        assert ks.activated_at is not None


# ── Operations ──────────────────────────────────────────────────────

class TestBackupManager:
    def test_backup(self, tmp_path):
        bm = BackupManager(storage_dir=tmp_path)
        ts = bm.backup("clickhouse")
        assert ts != ""
        assert bm.last_backup == ts

    def test_restore(self, tmp_path):
        bm = BackupManager(storage_dir=tmp_path)
        backup_id = bm.backup("clickhouse")
        assert bm.restore(backup_id) is True

    def test_verify(self, tmp_path):
        bm = BackupManager(storage_dir=tmp_path)
        result = bm.verify()
        assert result["rto_minutes"] == 120
        assert result["rpo_minutes"] == 60
        assert result["passed"] is True

    def test_default_retention(self):
        bm = BackupManager()
        assert bm.retention_days == 30


class TestFailoverManager:
    def test_default_active(self):
        fm = FailoverManager()
        assert fm.active == Region.PRIMARY

    def test_failover(self):
        fm = FailoverManager()
        assert fm.failover(Region.SECONDARY) is True
        assert fm.active == Region.SECONDARY

    def test_sync_status(self):
        fm = FailoverManager()
        status = fm.sync_status()
        assert status["active"] == "us-east"
        assert "lag_seconds" in status


# ── Backtest Optimized ──────────────────────────────────────────────

class TestVectorizedSharpe:
    def test_positive_returns(self):
        rng = np.random.RandomState(42)
        returns = rng.normal(0.001, 0.02, 252)
        sharpe = vectorized_sharpe(returns)
        assert sharpe > 0

    def test_zero_vol(self):
        returns = np.array([0.001] * 20)
        sharpe = vectorized_sharpe(returns)
        # vol=0 but code uses max(vol, 1e-10) → large sharpe due to tiny denom
        assert sharpe > 100  # effectively infinite sharpe


class TestVectorizedMDD:
    def test_upward_trend(self):
        equity = np.array([100, 101, 102, 103, 104, 105])
        mdd = vectorized_mdd(equity)
        assert mdd == 0.0

    def test_drawdown(self):
        equity = np.array([100, 90, 95, 105, 80, 110])
        mdd = vectorized_mdd(equity)
        assert mdd > 0

    def test_50pct_drawdown(self):
        equity = np.array([100, 50, 100])
        mdd = vectorized_mdd(equity)
        assert abs(mdd - 0.5) < 0.01


class TestVectorizedTradePnl:
    def test_basic(self):
        prices = np.array([100, 101, 102, 101, 100])
        signals = np.array([1, 1, 1, 0, -1])
        pnl = vectorized_trade_pnl(prices, signals)
        assert len(pnl) == 4
        # signal[0]=1 at price 100→101: return=(101-100)/100=0.01 → pnl=0.01
        assert abs(pnl[0] - 0.01) < 0.001
        # signal[1]=1 at price 101→102: return≈0.0099 → pnl≈0.0099
        assert abs(pnl[1] - 0.0099) < 0.001

    def test_all_long(self):
        prices = np.array([100, 101, 102, 103])
        signals = np.array([1, 1, 1, 1])
        pnl = vectorized_trade_pnl(prices, signals)
        assert len(pnl) == 3
        assert all(p > 0 for p in pnl)


# ── Factor Store ────────────────────────────────────────────────────

class TestFactorStore:
    def test_register(self):
        fs = FactorStore()
        fs.register(FactorDefinition(name="momentum"))
        assert fs.get("momentum") is not None

    def test_get_nonexistent(self):
        fs = FactorStore()
        assert fs.get("no_such_factor") is None

    def test_multiple_factors(self):
        fs = FactorStore()
        fs.register(FactorDefinition("mom"))
        fs.register(FactorDefinition("vol", dtype="float32"))
        assert len(fs.factors) == 2


class TestFactorDefinition:
    def test_defaults(self):
        fd = FactorDefinition(name="value")
        assert fd.name == "value"
        assert fd.entity == "symbol"
        assert fd.dtype == "float64"
        assert fd.refresh_interval == "daily"


# ── Impact Assessment ───────────────────────────────────────────────

class TestImpactAssessment:
    def test_create(self):
        ia = ImpactAssessment(
            change_id="CHG-001", max_potential_loss=50000,
            extreme_case_loss=200000,
        )
        assert ia.change_id == "CHG-001"
        assert ia.max_potential_loss == 50000
        assert not ia.approved

    def test_to_report(self):
        ia = ImpactAssessment(change_id="CHG-002", max_potential_loss=10000, approved=True)
        report = ia.to_report()
        assert report["max_loss"] == 10000
        assert report["approved"] is True

    def test_timestamp_set(self):
        ia = ImpactAssessment(change_id="CHG-003", max_potential_loss=0)
        assert ia.assessed_at != ""

    def test_default_liquidity_risk(self):
        ia = ImpactAssessment(change_id="CHG-004", max_potential_loss=1000)
        assert ia.liquidity_risk == "low"
