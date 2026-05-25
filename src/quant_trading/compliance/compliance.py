"""Tax and regulatory report domain models for compliance-001."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum

UTC = timezone.utc


class Jurisdiction(str, Enum):
    CN = "cn"
    US = "us"
    HK = "hk"
    EU = "eu"
    CRYPTO = "crypto"


@dataclass(frozen=True)
class TaxRule:
    stamp_duty_rate: float = 0.0
    capital_gains_rate: float = 0.0
    maker_fee_rate: float = 0.0
    taker_fee_rate: float = 0.0

    def __post_init__(self) -> None:
        for value in asdict(self).values():
            if not 0 <= value <= 1:
                raise ValueError("tax and fee rates must be between 0 and 1")


DEFAULT_TAX_RULES = {
    Jurisdiction.CN: TaxRule(stamp_duty_rate=0.0005),
    Jurisdiction.HK: TaxRule(stamp_duty_rate=0.001),
    Jurisdiction.US: TaxRule(capital_gains_rate=0.20),
    Jurisdiction.EU: TaxRule(capital_gains_rate=0.25),
    Jurisdiction.CRYPTO: TaxRule(maker_fee_rate=0.0002, taker_fee_rate=0.0005),
}


@dataclass(frozen=True)
class TaxCalculator:
    jurisdiction: Jurisdiction = Jurisdiction.CN
    rule: TaxRule | None = None

    @property
    def active_rule(self) -> TaxRule:
        return self.rule or DEFAULT_TAX_RULES[self.jurisdiction]

    def stamp_duty(self, notional: float) -> float:
        return round(max(0.0, notional) * self.active_rule.stamp_duty_rate, 2)

    def capital_gains(self, profit: float) -> float:
        return round(max(0.0, profit) * self.active_rule.capital_gains_rate, 2)

    def execution_fee(self, notional: float, liquidity: str = "taker") -> float:
        if liquidity not in {"maker", "taker"}:
            raise ValueError("liquidity must be maker or taker")
        rate = (
            self.active_rule.maker_fee_rate
            if liquidity == "maker"
            else self.active_rule.taker_fee_rate
        )
        return round(max(0.0, notional) * rate, 2)


@dataclass(frozen=True)
class ComplianceReport:
    """Compatibility report representation with canonical WORM bytes."""

    report_id: str
    jurisdiction: Jurisdiction
    period_start: date
    period_end: date
    total_trades: int = 0
    total_volume: float = 0.0
    stamp_duty_paid: float = 0.0
    capital_gains_tax: float = 0.0
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def __post_init__(self) -> None:
        if self.period_end < self.period_start:
            raise ValueError("period_end must be on or after period_start")

    def to_dict(self) -> dict:
        document = asdict(self)
        document["jurisdiction"] = self.jurisdiction.value
        document["period_start"] = self.period_start.isoformat()
        document["period_end"] = self.period_end.isoformat()
        return document

    def to_worm(self) -> bytes:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class TaxLossPosition:
    symbol: str
    quantity: float
    cost_basis: float
    market_price: float
    acquired_at: date


def suggest_tax_loss_harvesting(
    positions: list[TaxLossPosition],
    recent_buys: dict[str, date],
    as_of: date,
    minimum_loss: float = 0.0,
) -> list[dict]:
    """Suggest US loss realizations while excluding apparent wash sales."""
    suggestions = []
    for position in positions:
        loss = (position.cost_basis - position.market_price) * position.quantity
        last_buy = recent_buys.get(position.symbol)
        wash_sale_risk = last_buy is not None and as_of - timedelta(days=30) <= last_buy <= as_of
        if loss > minimum_loss and not wash_sale_risk:
            suggestions.append({"symbol": position.symbol, "realizable_loss": round(loss, 2)})
    return sorted(suggestions, key=lambda item: item["realizable_loss"], reverse=True)
