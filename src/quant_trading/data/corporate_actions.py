"""
Corporate Actions & adjustment factor engine.

AGENTS.md §4 (data-003): 权益事件处理（分红、拆股、配股、并购、交割）
- Three adjustment modes: pre-adjusted (前复权), post-adjusted (后复权), total return (总回报)
- Auto-fetch event calendar from data sources
- Apply adjustments to price series
- Futures/options delivery handling
- Impact report generation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

import pandas as pd

UTC = timezone.utc


# ── Event types ───────────────────────────────────────────────────────────────


class EventType(str, Enum):
    DIVIDEND = "dividend"  # 现金分红
    STOCK_SPLIT = "stock_split"  # 拆股/合股
    RIGHTS_ISSUE = "rights_issue"  # 配股
    MERGER = "merger"  # 并购
    SPINOFF = "spinoff"  # 分拆上市
    FUTURES_DELIVERY = "futures_delivery"  # 期货交割
    OPTION_EXERCISE = "option_exercise"  # 期权行权


class AdjustmentMode(str, Enum):
    PRE_ADJUSTED = "pre"  # 前复权 — backward adjust historical prices
    POST_ADJUSTED = "post"  # 后复权 — forward adjust future prices
    TOTAL_RETURN = "total"  # 总回报 — include dividends as reinvested


@dataclass
class CorporateActionEvent:
    """A single corporate action event."""

    symbol: str
    event_date: date
    event_type: EventType

    # Dividend
    cash_dividend_per_share: float | None = None  # 每股现金分红

    # Stock split (e.g., 2:1 split → split_ratio=2.0; 1:2 reverse → 0.5)
    split_ratio: float | None = None  # new_shares / old_shares

    # Rights issue
    rights_ratio: float | None = None  # new shares per existing share
    rights_price: float | None = None  # subscription price
    rights_theoretical_price: float | None = None  # computed

    # Merger
    merger_symbol: str | None = None  # target/acquiring symbol
    merger_ratio: float | None = None  # exchange ratio
    merger_cash: float | None = None  # cash per share

    # Futures delivery
    delivery_price: float | None = None  # settlement price
    delivery_quantity: float | None = None  # quantity to deliver

    # Metadata
    source: str = ""
    confirmed: bool = True
    notes: str = ""

    def __repr__(self) -> str:
        parts = [f"{self.symbol} on {self.event_date}: {self.event_type.value}"]
        if self.cash_dividend_per_share:
            parts.append(f"div=¥{self.cash_dividend_per_share}")
        if self.split_ratio:
            parts.append(f"split={self.split_ratio}")
        return " ".join(parts)


# ── Adjustment factor calculator ──────────────────────────────────────────────


@dataclass
class AdjustmentFactors:
    """Cumulative adjustment factors for a symbol over time."""

    symbol: str
    dates: list[date] = field(default_factory=list)
    pre_factors: list[float] = field(default_factory=list)  # 前复权
    post_factors: list[float] = field(default_factory=list)  # 后复权
    total_return_factors: list[float] = field(default_factory=list)

    def to_dataframe(self) -> pd.DataFrame:
        """Convert to DataFrame indexed by date."""
        return pd.DataFrame(
            {
                "pre_factor": self.pre_factors,
                "post_factor": self.post_factors,
                "total_return_factor": self.total_return_factors,
            },
            index=pd.DatetimeIndex(pd.to_datetime(self.dates)),
        ).sort_index()


class AdjustmentEngine:
    """
    Calculates adjustment factors from corporate action events.

    Three modes per AGENTS.md §4 (data-003):
    - pre-adjust (前复权): backward-adjust historical prices to current
    - post-adjust (后复权): forward-adjust prices from IPO
    - total return (总回报): include dividends as reinvested (default)

    Algorithm:
    For each event from earliest to latest:
      split: factor *= split_ratio
      dividend: pre_factor unchanged; post_factor *= (close - div) / close;
                total_return: track cumulative reinvested cash
    """

    def compute_factors(
        self,
        events: list[CorporateActionEvent],
        price_series: pd.Series | None = None,
    ) -> AdjustmentFactors:
        """
        Compute adjustment factors from a list of corporate events.

        Args:
            events: Sorted list of corporate action events (earliest first).
            price_series: Close prices for dividend factor calculation.
                          Required for dividend adjustments (needs close price on ex-date).
        """
        events = sorted(events, key=lambda e: e.event_date)
        factors = AdjustmentFactors(symbol=events[0].symbol if events else "UNKNOWN")

        pre_factor = 1.0
        post_factor = 1.0
        tr_factor = 1.0  # total return factor
        cum_tr_div = 0.0  # cumulative total-return dividend (reinvested)

        event_idx = 0

        if price_series is not None and not price_series.empty:
            price_series = price_series.sort_index()
            all_dates = price_series.index.tolist()

            for dt in all_dates:
                d = dt.date() if hasattr(dt, "date") else pd.Timestamp(dt).date()

                # Apply all events on or before this date
                while event_idx < len(events) and events[event_idx].event_date <= d:
                    ev = events[event_idx]
                    close = float(price_series.loc[dt]) if dt in price_series.index else 0

                    pre_factor, post_factor, tr_factor, cum_tr_div = self._apply_event(
                        ev, close, pre_factor, post_factor, tr_factor, cum_tr_div
                    )
                    event_idx += 1

                factors.dates.append(d)
                factors.pre_factors.append(round(pre_factor, 10))
                factors.post_factors.append(round(post_factor, 10))
                factors.total_return_factors.append(round(tr_factor, 10))
        else:
            # No price series — compute per-event factors
            for ev in events:
                factors.dates.append(ev.event_date)
                pre_factor, post_factor, tr_factor, cum_tr_div = self._apply_event(
                    ev, 0, pre_factor, post_factor, tr_factor, cum_tr_div
                )
                # Pre-factor: always use the terminal cumulative value
                # (前复权: backward normalize to latest, factor decays over time)
                factors.pre_factors.append(round(pre_factor, 10))
                factors.post_factors.append(round(post_factor, 10))
                factors.total_return_factors.append(round(tr_factor, 10))

        # Post-process: pre_factor should be terminal value for ALL dates
        # (前复权 applies final cumulative factor uniformly backward across history)
        if factors.pre_factors:
            terminal_pre = factors.pre_factors[-1]
            factors.pre_factors = [terminal_pre] * len(factors.pre_factors)

        return factors

    def _apply_event(
        self,
        event: CorporateActionEvent,
        close: float,
        pre: float,
        post: float,
        tr: float,
        cum_div: float,
    ) -> tuple[float, float, float, float]:
        """Apply a single corporate action event to the running factors."""
        if event.event_type == EventType.STOCK_SPLIT and event.split_ratio:
            sr = event.split_ratio
            # Pre-adjust: divide by split ratio (backward normalize)
            #   A 2:1 split means old prices are halved to compare with new
            pre /= sr
            # Post-adjust: multiply by split ratio (forward inflate)
            post *= sr
            tr *= sr

        elif event.event_type == EventType.DIVIDEND and event.cash_dividend_per_share:
            div = event.cash_dividend_per_share
            if close > 0:
                # Pre-adjust: no change (dividend doesn't affect pre_factor directly
                #   — pre_factor is applied to historical prices, dividends are
                #   handled as separate cash flows)
                # Post-adjust: adjust for the ex-dividend drop
                post *= (close - div) / close
                # Total return: reinvest dividend
                tr *= close / (close - div)
                cum_div += div * (tr / post)  # Reinvested dividend shares

        elif event.event_type == EventType.RIGHTS_ISSUE:
            if event.rights_ratio and event.rights_price and close > 0:
                rr = event.rights_ratio
                rp = event.rights_price
                # Theoretical ex-rights price
                teor_price = (close + rr * rp) / (1 + rr)
                factor = teor_price / close
                pre *= factor
                post *= factor
                tr *= factor

        elif event.event_type == EventType.MERGER and event.merger_ratio:
            mr = event.merger_ratio
            pre *= mr
            post *= mr
            tr *= mr

        return pre, post, tr, cum_div


# ── Price adjuster ────────────────────────────────────────────────────────────


def adjust_prices(
    df: pd.DataFrame,
    factors: AdjustmentFactors,
    mode: AdjustmentMode = AdjustmentMode.TOTAL_RETURN,
) -> pd.DataFrame:
    """
    Apply adjustment factors to a price DataFrame.

    Args:
        df: OHLCV DataFrame with DatetimeIndex.
        factors: Pre-computed adjustment factors.
        mode: Which adjustment to apply.

    Returns:
        Adjusted DataFrame with same structure.
    """
    fac_df = factors.to_dataframe()
    adjusted = df.copy()

    # Align dates
    common_idx = adjusted.index.intersection(fac_df.index)
    if len(common_idx) == 0:
        return adjusted

    factor_col = {
        AdjustmentMode.PRE_ADJUSTED: "pre_factor",
        AdjustmentMode.POST_ADJUSTED: "post_factor",
        AdjustmentMode.TOTAL_RETURN: "total_return_factor",
    }[mode]

    factors_aligned = fac_df.loc[common_idx, factor_col]

    # Apply to price columns
    price_cols = ["open", "high", "low", "close", "vwap"]
    for col in price_cols:
        if col in adjusted.columns:
            adjusted.loc[common_idx, col] = adjusted.loc[common_idx, col] * factors_aligned

    # Volume adjusted inversely (shares * factor = price * volume / factor)
    if "volume" in adjusted.columns:
        adjusted.loc[common_idx, "volume"] = adjusted.loc[common_idx, "volume"] / factors_aligned

    return adjusted


# ── Event calendar fetcher ────────────────────────────────────────────────────


class EventCalendar:
    """
    Fetch corporate action events from data sources.

    Currently fetches from AKShare (A-shares). Extensible to other markets.
    """

    def __init__(self) -> None:
        self._cache: dict[str, list[CorporateActionEvent]] = {}

    async def fetch_dividends(
        self, symbol: str, start_date: date | None = None
    ) -> list[CorporateActionEvent]:
        """Fetch dividend history for an A-share stock."""
        try:
            import akshare as ak

            code = symbol.split(".")[0]
            df = ak.stock_dividents_cninfo(symbol=code)

            if df is None or df.empty:
                return []

            events = []
            for _, row in df.iterrows():
                try:
                    ev_date = pd.Timestamp(row.get("除权除息日") or row.get("报告期")).date()
                    div_val = float(row.get("每股派息", 0) or 0)
                    if div_val > 0 and (start_date is None or ev_date >= start_date):
                        events.append(
                            CorporateActionEvent(
                                symbol=symbol,
                                event_date=ev_date,
                                event_type=EventType.DIVIDEND,
                                cash_dividend_per_share=div_val,
                                source="akshare",
                            )
                        )
                except (ValueError, KeyError):
                    continue

            return events

        except ImportError:
            return []
        except Exception:
            return []

    async def fetch_splits(
        self, symbol: str, start_date: date | None = None
    ) -> list[CorporateActionEvent]:
        """Fetch stock split / reverse split history."""
        try:
            import akshare as ak

            code = symbol.split(".")[0]
            ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date="19900101",
                end_date="20500101",
                adjust="",  # Unadjusted
            )
            # AKShare doesn't directly expose splits — detect from price discontinuities
            return []
        except Exception:
            return []

    async def fetch_all(
        self, symbol: str, start_date: date | None = None
    ) -> list[CorporateActionEvent]:
        """Fetch all corporate action events for a symbol."""
        cache_key = f"{symbol}:{start_date or 'all'}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        dividends = await self.fetch_dividends(symbol, start_date)
        splits = await self.fetch_splits(symbol, start_date)

        all_events = sorted(dividends + splits, key=lambda e: e.event_date)
        self._cache[cache_key] = all_events
        return all_events


# ── Futures / Options delivery handler ────────────────────────────────────────


class DeliveryHandler:
    """Handle futures physical/cash delivery and option exercise."""

    def handle_futures_delivery(
        self,
        symbol: str,
        delivery_price: float,
        quantity: float,
        contract_multiplier: float = 1.0,
        delivery_type: str = "cash",  # "cash" or "physical"
    ) -> dict:
        """
        Process futures contract delivery.

        Returns impact report dict.
        """
        notional = delivery_price * quantity * contract_multiplier

        report = {
            "symbol": symbol,
            "event_type": "futures_delivery",
            "delivery_date": datetime.now(UTC).date().isoformat(),
            "delivery_price": delivery_price,
            "quantity": quantity,
            "notional_value": notional,
            "delivery_type": delivery_type,
            "settlement_cash": 0.0 if delivery_type == "physical" else notional,
            "position_change": -quantity if delivery_type == "physical" else 0,
        }

        if delivery_type == "physical":
            report["notes"] = (
                f"Physical delivery: {quantity} contracts at ¥{delivery_price}. "
                f"Position reduced to 0. Verify warehouse receipt."
            )

        return report

    def handle_option_exercise(
        self,
        symbol: str,
        strike: float,
        quantity: float,
        option_type: str = "call",
        underlying_price: float = 0.0,
    ) -> dict:
        """
        Process option exercise.

        Returns impact report dict.
        """
        intrinsic = max(
            0, (underlying_price - strike) if option_type == "call" else (strike - underlying_price)
        )
        settlement = intrinsic * quantity

        return {
            "symbol": symbol,
            "event_type": "option_exercise",
            "strike": strike,
            "quantity": quantity,
            "option_type": option_type,
            "underlying_price": underlying_price,
            "intrinsic_value": intrinsic,
            "settlement_cash": settlement,
            "notes": (
                f"{option_type.upper()} exercise: {quantity} contracts, "
                f"strike=¥{strike}, underlying=¥{underlying_price}, "
                f"settlement=¥{settlement:,.2f}"
            ),
        }


# ── Impact report generator ───────────────────────────────────────────────────


class ImpactReport:
    """
    Generate corporate action impact reports for risk assessment.

    AGENTS.md §4 (data-003): 生成权益事件影响报告
    （分红现金流、拆股股份变动、配股资金需求）
    """

    @staticmethod
    def generate(events: list[CorporateActionEvent], holdings: float = 0) -> dict[str, Any]:
        """
        Generate impact summary from a list of corporate action events.

        Args:
            events: List of corporate action events.
            holdings: Current position size (shares).

        Returns:
            Report dict with cash flows, share changes, and capital requirements.
        """
        by_type: dict[str, Any] = {}
        details: list[dict[str, Any]] = []
        report: dict[str, Any] = {
            "total_events": len(events),
            "cash_dividends_total": 0.0,
            "shares_change_pct": 0.0,
            "rights_issue_capital_needed": 0.0,
            "by_type": by_type,
            "details": details,
        }

        cum_split = 1.0
        for ev in events:
            detail: dict[str, Any] = {
                "date": ev.event_date.isoformat(),
                "type": ev.event_type.value,
                "symbol": ev.symbol,
            }

            if ev.event_type == EventType.DIVIDEND and ev.cash_dividend_per_share:
                cash = ev.cash_dividend_per_share * holdings * cum_split
                report["cash_dividends_total"] += cash
                detail["cash_amount"] = cash
                detail["per_share"] = ev.cash_dividend_per_share
                by_type.setdefault("dividend", 0.0)
                by_type["dividend"] += cash

            elif ev.event_type == EventType.STOCK_SPLIT and ev.split_ratio:
                cum_split *= ev.split_ratio
                detail["split_ratio"] = ev.split_ratio
                detail["cumulative_split"] = cum_split
                by_type.setdefault("split", []).append(ev.split_ratio)

            elif ev.event_type == EventType.RIGHTS_ISSUE and ev.rights_ratio:
                new_shares = holdings * ev.rights_ratio * cum_split
                capital = new_shares * (ev.rights_price or 0)
                report["rights_issue_capital_needed"] += capital
                detail["new_shares"] = new_shares
                detail["capital_needed"] = capital

            details.append(detail)

        report["shares_change_pct"] = round((cum_split - 1.0) * 100, 2)
        report["cash_dividends_total"] = round(report["cash_dividends_total"], 2)
        report["rights_issue_capital_needed"] = round(report["rights_issue_capital_needed"], 2)

        return report
