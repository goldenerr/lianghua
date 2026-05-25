"""
Market adapter routing & futures contract rollover.

AGENTS.md §2 (config-002):
- Data sources select by primary_markets
- Risk rules vary by market type
- Fee/tax models switch by market
- Futures: auto-rollover on dominant contract switch
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from .market_calendar import CalendarRegistry, Market, MarketCalendar

logger = logging.getLogger(__name__)


# ── Market rules (validated from config) ──────────────────────────────────────


class MarketRuleSet:
    """Per-market trading rules loaded from config/system.yaml -> market_rules."""

    def __init__(
        self,
        market: Market,
        tick_size: float = 0.01,
        lot_size: int = 100,
        price_precision: int = 2,
        funding_rate: float | None = None,
        settlement_time_utc: str | None = None,
    ):
        self.market = market
        self.tick_size = tick_size
        self.lot_size = lot_size
        self.price_precision = price_precision
        self.funding_rate = funding_rate
        self.settlement_time_utc = settlement_time_utc

    def round_price(self, price: float) -> float:
        """Round price to market tick size. Uses decimal-safe rounding."""
        ticks = round(price / self.tick_size)
        return ticks * self.tick_size

    def round_quantity(self, qty: float) -> float:
        """Round quantity to lot size multiples."""
        return round(qty / self.lot_size) * self.lot_size


# Default rule sets per market
DEFAULT_RULES: dict[Market, MarketRuleSet] = {
    Market.A_SHARES: MarketRuleSet(Market.A_SHARES, tick_size=0.01, lot_size=100, price_precision=2),
    Market.FUTURES: MarketRuleSet(Market.FUTURES, tick_size=1.0, lot_size=1, price_precision=0),
    Market.CRYPTO: MarketRuleSet(Market.CRYPTO, tick_size=0.01, lot_size=1, price_precision=2, funding_rate=0.0001),
    Market.US_STOCKS: MarketRuleSet(Market.US_STOCKS, tick_size=0.01, lot_size=1, price_precision=2),
    Market.HK_STOCKS: MarketRuleSet(Market.HK_STOCKS, tick_size=0.01, lot_size=100, price_precision=2),
    Market.OPTIONS: MarketRuleSet(Market.OPTIONS, tick_size=0.01, lot_size=1, price_precision=2),
}

# Fee models (placeholder — expanded in compliance-001)
FEE_MODELS: dict[Market, str] = {
    Market.A_SHARES: "cn_stock",       # 印花税 0.05% + 佣金
    Market.FUTURES: "cn_futures",      # 交易所手续费
    Market.CRYPTO: "crypto_maker_taker",
    Market.US_STOCKS: "us_stock",
    Market.HK_STOCKS: "hk_stock",
}


# ── Futures contract rollover ─────────────────────────────────────────────────


class ContractMonth:
    """Futures contract month identifier (e.g., IF2406 = IF + 2024-06)."""

    def __init__(self, symbol: str):
        # Parse "IF2406" → underlying="IF", year=2024, month=6
        self.underlying = symbol[:-4]
        self.year = int("20" + symbol[-4:-2])
        self.month = int(symbol[-2:])
        self.code = symbol

    @property
    def expiry_date(self) -> date:
        """Estimated expiry: 3rd Friday of contract month (CFFEX)."""
        import calendar as cal_mod

        # Third Friday
        c = cal_mod.monthcalendar(self.year, self.month)
        # Fridays are weekday 4; find third occurrence
        fridays = [week[4] for week in c if week[4] != 0]
        day = fridays[2] if len(fridays) >= 3 else fridays[-1]
        return date(self.year, self.month, day)

    def __repr__(self) -> str:
        return f"ContractMonth({self.code})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ContractMonth):
            return False
        return self.code == other.code

    def __hash__(self) -> int:
        return hash(self.code)


class FuturesRolloverDetector:
    """
    Detects dominant contract switches for Chinese futures.

    AGENTS.md §57 (config-002): 主力合约切换时自动平旧仓、开新仓，记录换月成本。

    Rollover trigger: volume of next contract > current dominant
    for 3 consecutive days (configurable).
    """

    def __init__(
        self,
        underlying: str,
        consecutive_days: int = 3,
    ):
        self.underlying = underlying
        self.consecutive_days = consecutive_days
        self._dominant: ContractMonth | None = None
        self._volume_history: dict[ContractMonth, int] = {}
        self._next_contract_lead_days: int = 0

    @property
    def dominant_contract(self) -> ContractMonth | None:
        return self._dominant

    def update_volume(self, contract: ContractMonth, volume: float) -> None:
        """Update volume data and check for rollover signal."""
        self._volume_history[contract] = self._volume_history.get(contract, 0) + int(volume)

        # Determine current dominant (highest volume last 5 days)
        # Simplified: just track latest dominant
        if self._dominant is None:
            self._dominant = contract
            return

    def check_rollover(
        self, contracts: list[tuple[ContractMonth, float]], today: date
    ) -> tuple[ContractMonth, ContractMonth] | None:
        """
        Check if dominant should switch.

        Args:
            contracts: List of (contract, today_volume) tuples.
            today: Current date.

        Returns:
            (old_dominant, new_dominant) if rollover detected, else None.
        """
        if not contracts or self._dominant is None:
            return None

        # Update volumes
        max_vol = 0.0
        max_contract = self._dominant
        for ct, vol in contracts:
            self.update_volume(ct, vol)
            if vol > max_vol:
                max_vol = vol
                max_contract = ct

        # Check if next contract overtakes
        if max_contract != self._dominant:
            self._next_contract_lead_days += 1
        else:
            self._next_contract_lead_days = 0

        if self._next_contract_lead_days >= self.consecutive_days:
            old = self._dominant
            new = max_contract
            self._dominant = new
            self._next_contract_lead_days = 0
            logger.info(
                "Futures rollover: %s → %s (underlying=%s, date=%s)",
                old.code, new.code, self.underlying, today,
            )
            return (old, new)

        return None

    def rollover_cost_estimate(
        self, old: ContractMonth, new: ContractMonth, position: float, current_price: float
    ) -> dict[str, float]:
        """
        Estimate the cost of rolling over a futures position.

        Returns:
            dict with estimated spread cost, commission, and total.
        """
        # Simplified: assume 0.05% spread cost + 2x commission
        spread_cost = position * current_price * 0.0005
        commission = position * current_price * 0.00005 * 2  # close old + open new
        return {
            "spread_cost": round(spread_cost, 2),
            "commission": round(commission, 2),
            "total": round(spread_cost + commission, 2),
        }


# ── Market adapter router ─────────────────────────────────────────────────────


class MarketRouter:
    """
    Routes market-specific behavior: data sources, risk rules, fee models.

    AGENTS.md §2 (config-002):
      "数据源根据 primary_markets 自动选择合适的数据接口"
      "风控模块根据市场类型启用不同规则"
      "税费计算根据市场自动切换费率模型"
    """

    # Data source mappings per market (expanded in data-001)
    DATA_SOURCE_PRIORITY: dict[Market, list[str]] = {
        Market.A_SHARES: ["tushare", "akshare", "yfinance"],
        Market.FUTURES: ["tushare", "akshare", "ccxt"],
        Market.CRYPTO: ["ccxt", "binance"],
        Market.US_STOCKS: ["yfinance", "polygon"],
        Market.HK_STOCKS: ["yfinance", "akshare"],
        Market.OPTIONS: ["yfinance"],
    }

    @classmethod
    def get_data_sources(cls, market: Market) -> list[str]:
        """Get ordered list of data source names for a market."""
        return cls.DATA_SOURCE_PRIORITY.get(market, ["yfinance"])

    @classmethod
    def get_fee_model(cls, market: Market) -> str:
        """Get fee/tax model identifier for a market."""
        return FEE_MODELS.get(market, "default")

    @classmethod
    def get_rules(cls, market: Market) -> MarketRuleSet:
        """Get trading rules for a market."""
        return DEFAULT_RULES.get(market, MarketRuleSet(market))

    @classmethod
    def get_calendar(cls, market: Market) -> MarketCalendar:
        """Get trading calendar for a market."""
        return CalendarRegistry.get(market)

    @classmethod
    def is_trading_allowed(
        cls,
        market: Market,
        dt: datetime | None = None,
        *,
        allow_settlement_window: bool = False,
    ) -> tuple[bool, str]:
        """
        Check if trading is allowed for a market at a given time.

        Returns:
            (allowed, reason) tuple.
        """
        cal = cls.get_calendar(market)
        check_dt = dt or datetime.now()

        if not cal.is_in_session(check_dt):
            return False, f"{market.value} is not in trading session"

        from .market_calendar import SettlementWindow

        if not allow_settlement_window and SettlementWindow.is_in_settlement_window(
            market, check_dt
        ):
            return False, f"{market.value} is in settlement window (new positions restricted)"

        return True, "ok"
