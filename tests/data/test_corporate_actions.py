"""Tests for corporate actions engine."""
import pytest
import pandas as pd
from datetime import date

from quant_trading.data.corporate_actions import (
    CorporateActionEvent,
    EventType,
    AdjustmentMode,
    AdjustmentEngine,
    AdjustmentFactors,
    adjust_prices,
    DeliveryHandler,
    ImpactReport,
)


# ── AdjustmentEngine ──────────────────────────────────────────────────────

class TestAdjustmentEngine:
    def test_no_events_returns_identity(self):
        engine = AdjustmentEngine()
        factors = engine.compute_factors([], None)
        assert factors.pre_factors == []

    def test_stock_split_adjustment(self):
        engine = AdjustmentEngine()
        events = [
            CorporateActionEvent(
                symbol="TEST", event_date=date(2024, 6, 15),
                event_type=EventType.STOCK_SPLIT, split_ratio=2.0
            )
        ]
        factors = engine.compute_factors(events, None)
        # Pre: 1/2 = 0.5 (backward normalize), Post: 2.0 (forward inflate)
        assert factors.pre_factors[-1] == pytest.approx(0.5)
        assert factors.post_factors[-1] == pytest.approx(2.0)

    def test_multiple_splits_compound(self):
        engine = AdjustmentEngine()
        events = [
            CorporateActionEvent(symbol="T", event_date=date(2024, 1, 1),
                                 event_type=EventType.STOCK_SPLIT, split_ratio=2.0),
            CorporateActionEvent(symbol="T", event_date=date(2024, 6, 1),
                                 event_type=EventType.STOCK_SPLIT, split_ratio=3.0),
        ]
        factors = engine.compute_factors(events, None)
        assert factors.pre_factors[-1] == pytest.approx(1/6)  # 1/2 * 1/3
        assert factors.post_factors[-1] == pytest.approx(6.0)  # 2 * 3

    def test_dividend_adjustment_with_price(self):
        engine = AdjustmentEngine()
        # Price on ex-date = 10.0, dividend = 0.5
        # Post-factor = (10 - 0.5) / 10 = 0.95
        # Total-return factor = 10 / (10 - 0.5) = 1.05263...
        events = [
            CorporateActionEvent(
                symbol="T",
                event_date=date(2024, 6, 15),
                event_type=EventType.DIVIDEND,
                cash_dividend_per_share=0.5,
            )
        ]
        prices = pd.Series(
            [10.0, 10.0],
            index=pd.DatetimeIndex(pd.to_datetime(["2024-01-01", "2024-06-15"])),
        )
        factors = engine.compute_factors(events, prices)
        assert factors.pre_factors[-1] == 1.0  # Pre-adjust: unchanged
        assert factors.post_factors[-1] == pytest.approx(0.95)
        assert factors.total_return_factors[-1] == pytest.approx(1.0526315789)

    def test_split_before_dividend(self):
        engine = AdjustmentEngine()
        events = [
            CorporateActionEvent(symbol="T", event_date=date(2024, 3, 1),
                                 event_type=EventType.STOCK_SPLIT, split_ratio=2.0),
            CorporateActionEvent(symbol="T", event_date=date(2024, 6, 1),
                                 event_type=EventType.DIVIDEND,
                                 cash_dividend_per_share=1.0),
        ]
        prices = pd.Series(
            [10.0, 10.0],
            index=pd.DatetimeIndex(pd.to_datetime(["2024-01-01", "2024-06-01"])),
        )
        factors = engine.compute_factors(events, prices)
        # Split: pre /= 2 → 0.5, post *= 2 → 2.0
        # Dividend: pre unchanged (0.5), post *= (10-1)/10 = 0.9 → 2.0*0.9 = 1.8
        assert factors.pre_factors[-1] == pytest.approx(0.5)
        assert factors.post_factors[-1] == pytest.approx(1.8)

    def test_factors_to_dataframe(self):
        engine = AdjustmentEngine()
        events = [
            CorporateActionEvent(symbol="T", event_date=date(2024, 6, 15),
                                 event_type=EventType.STOCK_SPLIT, split_ratio=2.0),
        ]
        factors = engine.compute_factors(events, None)
        df = factors.to_dataframe()
        assert "pre_factor" in df.columns
        assert "post_factor" in df.columns
        assert "total_return_factor" in df.columns
        assert df["pre_factor"].iloc[-1] == pytest.approx(0.5)
        assert df["post_factor"].iloc[-1] == pytest.approx(2.0)


# ── adjust_prices ─────────────────────────────────────────────────────────

class TestAdjustPrices:
    def test_pre_adjust(self):
        df = pd.DataFrame(
            {"open": [10.0, 10.0], "high": [11.0, 11.0],
             "low": [9.0, 9.0], "close": [10.0, 10.0], "volume": [1000.0, 1000.0]},
            index=pd.DatetimeIndex(pd.to_datetime(["2024-01-01", "2024-06-15"])),
        )
        engine = AdjustmentEngine()
        events = [
            CorporateActionEvent(symbol="T", event_date=date(2024, 3, 1),
                                 event_type=EventType.STOCK_SPLIT, split_ratio=2.0),
        ]
        # Use price series so factor dates align with price dates
        prices = pd.Series(df["close"].values, index=df.index)
        factors = engine.compute_factors(events, prices)
        adjusted = adjust_prices(df, factors, mode=AdjustmentMode.PRE_ADJUSTED)
        # Pre-adjust: pre-factor after split = 0.5. Both dates: 10*0.5 = 5.0
        assert adjusted["close"].iloc[-1] == pytest.approx(5.0)
        assert adjusted["close"].iloc[0] == pytest.approx(5.0)
        # Volume adjusted inversely: 1000 / 0.5 = 2000
        assert adjusted["volume"].iloc[-1] == pytest.approx(2000.0)

    def test_total_return(self):
        df = pd.DataFrame(
            {"close": [10.0, 10.0], "volume": [1000.0, 1000.0]},
            index=pd.DatetimeIndex(pd.to_datetime(["2024-06-14", "2024-06-15"])),
        )
        engine = AdjustmentEngine()
        events = [
            CorporateActionEvent(symbol="T", event_date=date(2024, 6, 15),
                                 event_type=EventType.DIVIDEND,
                                 cash_dividend_per_share=0.5),
        ]
        prices = pd.Series([10.0, 10.0], index=df.index)
        factors = engine.compute_factors(events, prices)
        adjusted = adjust_prices(df, factors, mode=AdjustmentMode.TOTAL_RETURN)
        assert adjusted["close"].iloc[-1] == pytest.approx(10.5263157894)


# ── CorporateActionEvent ──────────────────────────────────────────────────

class TestCorporateActionEvent:
    def test_repr(self):
        ev = CorporateActionEvent(
            symbol="600519.SH",
            event_date=date(2024, 6, 15),
            event_type=EventType.DIVIDEND,
            cash_dividend_per_share=1.5,
        )
        r = repr(ev)
        assert "600519" in r
        assert "dividend" in r
        assert "1.5" in r

    def test_defaults(self):
        ev = CorporateActionEvent(
            symbol="TEST", event_date=date(2024, 1, 1),
            event_type=EventType.STOCK_SPLIT, split_ratio=1.0,
        )
        assert ev.confirmed is True
        assert ev.source == ""


# ── DeliveryHandler ───────────────────────────────────────────────────────

class TestDeliveryHandler:
    def test_cash_delivery(self):
        handler = DeliveryHandler()
        report = handler.handle_futures_delivery(
            "IF2406", delivery_price=3500.0, quantity=2,
            contract_multiplier=300.0, delivery_type="cash",
        )
        assert report["notional_value"] == pytest.approx(2_100_000)
        assert report["settlement_cash"] == pytest.approx(2_100_000)

    def test_physical_delivery(self):
        handler = DeliveryHandler()
        report = handler.handle_futures_delivery(
            "CU2406", delivery_price=68000.0, quantity=5,
            contract_multiplier=5.0, delivery_type="physical",
        )
        assert report["position_change"] == -5
        assert report["settlement_cash"] == 0

    def test_call_option_exercise(self):
        handler = DeliveryHandler()
        report = handler.handle_option_exercise(
            "IO2406-C-3500", strike=3500.0, quantity=3,
            option_type="call", underlying_price=3600.0,
        )
        assert report["intrinsic_value"] == pytest.approx(100.0)
        assert report["settlement_cash"] == pytest.approx(300.0)

    def test_otm_put(self):
        handler = DeliveryHandler()
        report = handler.handle_option_exercise(
            "IO2406-P-3500", strike=3500.0, quantity=3,
            option_type="put", underlying_price=3600.0,
        )
        assert report["intrinsic_value"] == 0
        assert report["settlement_cash"] == 0


# ── ImpactReport ──────────────────────────────────────────────────────────

class TestImpactReport:
    def test_dividend_impact(self):
        events = [
            CorporateActionEvent(
                symbol="600519.SH",
                event_date=date(2024, 6, 1),
                event_type=EventType.DIVIDEND,
                cash_dividend_per_share=10.0,
            )
        ]
        report = ImpactReport.generate(events, holdings=1000)
        assert report["cash_dividends_total"] == pytest.approx(10000.0)
        assert report["total_events"] == 1

    def test_split_impact(self):
        events = [
            CorporateActionEvent(
                symbol="T", event_date=date(2024, 6, 1),
                event_type=EventType.STOCK_SPLIT, split_ratio=2.0,
            )
        ]
        report = ImpactReport.generate(events, holdings=1000)
        assert report["shares_change_pct"] == pytest.approx(100.0)

    def test_mixed_events(self):
        events = [
            CorporateActionEvent(
                symbol="T", event_date=date(2024, 3, 1),
                event_type=EventType.STOCK_SPLIT, split_ratio=3.0),
            CorporateActionEvent(
                symbol="T", event_date=date(2024, 6, 1),
                event_type=EventType.DIVIDEND, cash_dividend_per_share=0.5),
        ]
        report = ImpactReport.generate(events, holdings=1000)
        assert report["shares_change_pct"] == pytest.approx(200.0)
        # Dividend on split-adjusted basis: 1000 * 3 * 0.5 = 1500
        assert report["cash_dividends_total"] == pytest.approx(1500.0)

    def test_rights_issue_capital(self):
        events = [
            CorporateActionEvent(
                symbol="T", event_date=date(2024, 6, 1),
                event_type=EventType.RIGHTS_ISSUE,
                rights_ratio=0.3, rights_price=5.0,
            )
        ]
        report = ImpactReport.generate(events, holdings=1000)
        assert report["rights_issue_capital_needed"] == pytest.approx(1500.0)
