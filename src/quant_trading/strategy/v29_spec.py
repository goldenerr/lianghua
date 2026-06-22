"""Validated V29 strategy launch specification.

The V29 profitable candidate must be promoted through a stable, auditable
contract before a paper-trading service can load it. This module validates that
contract and keeps live order submission fail-closed by construction.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from quant_trading.config.settings import Market

PAPER_START_REQUIRED_KEYS: tuple[str, ...] = (
    "external_worm_archive",
    "secret_manager",
    "approval_service",
    "real_market_data_provider",
    "provider_entitlement",
    "exchange_position_provider",
    "signed_plugin_review",
    "production_calendar",
    "capacity_benchmark",
)

V29_PRICE_ONLY_NOT_USED_KEYS: tuple[str, ...] = (
    "alt_archive_readiness",
    "broker_borrow_availability",
    "rollover_runbook",
)

ALLOWED_SLEEVES: frozenset[str] = frozenset(
    {
        "price_ic_alpha",
        "price_ic_diversified",
        "price_lowvol_reversal",
        "price_defensive_breadth",
    }
)


class V29InputRefs(BaseModel):
    """Externalized input references for the V29 paper candidate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    universe_ref: str = Field(min_length=1)
    price_history_dir: str = Field(min_length=1)
    industry_classification_ref: str = Field(min_length=1)
    carry_floor_asset_ref: str = Field(min_length=1)
    readiness_ref: str = Field(min_length=1)
    research_result_ref: str = Field(min_length=1)
    robustness_ref: str = Field(min_length=1)
    launch_package_ref: str = Field(min_length=1)


class V29CandidateParameters(BaseModel):
    """Research-locked V29 portfolio-construction parameters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sleeves: tuple[str, ...] = Field(min_length=1)
    lookback: int = Field(ge=20, le=756)
    rebalance_freq: int = Field(ge=1, le=60)
    base_gross: float = Field(ge=0, le=1)
    bull_gross: float = Field(ge=0, le=1)
    neutral_gross: float = Field(ge=0, le=1)
    risk_off_gross: float = Field(ge=0, le=1)
    vol_target: float = Field(gt=0, le=1)
    max_sleeve_weight: float = Field(gt=0, le=1)
    temperature: float = Field(gt=0, le=10)
    dd_reduce_threshold: float = Field(gt=0, le=1)
    dd_reduce_scale: float = Field(ge=0, le=1)
    dd_stop_threshold: float = Field(gt=0, le=1)
    dd_stop_scale: float = Field(ge=0, le=1)
    meta_cost_bps: float = Field(ge=0, le=100)
    use_crisis_sleeve: bool
    crisis_fraction: float = Field(ge=0, le=1)
    carry_fraction: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_v29_price_only_scope(self) -> V29CandidateParameters:
        unknown = sorted(set(self.sleeves) - ALLOWED_SLEEVES)
        if unknown:
            raise ValueError(f"unsupported V29 sleeve(s): {unknown}")
        if self.dd_reduce_threshold >= self.dd_stop_threshold:
            raise ValueError("dd_reduce_threshold must be below dd_stop_threshold")
        if self.crisis_fraction + self.carry_fraction > 1.0:
            raise ValueError("crisis_fraction + carry_fraction must not exceed 1")
        return self


class V29ResearchMetrics(BaseModel):
    """Minimum metrics that justify starting the approved paper stage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    full_sharpe: float = Field(ge=2.0)
    avg_oos_sharpe: float = Field(ge=1.5)
    min_oos_sharpe: float = Field(ge=0.0)
    max_drawdown: float = Field(ge=-0.10, le=0.0)
    win_rate: float = Field(ge=0.50, le=1.0)
    wf_folds: int = Field(ge=5)


class V29LaunchControls(BaseModel):
    """Fail-closed controls for the V29 paper-trading launch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    auto_trade_enabled: Literal[False] = False
    live_order_submission_allowed: Literal[False] = False
    max_live_capital_fraction: Literal[0.0] = 0.0
    paper_min_calendar_days: int = Field(default=90, ge=90)
    paper_min_performance_vs_backtest: float = Field(default=0.70, ge=0.70, le=1.0)
    fail_closed_on_missing_evidence: Literal[True] = True
    required_evidence_for_paper_start: tuple[str, ...] = PAPER_START_REQUIRED_KEYS
    not_used_external_keys_for_candidate_scope: tuple[str, ...] = V29_PRICE_ONLY_NOT_USED_KEYS

    @model_validator(mode="after")
    def validate_evidence_contract(self) -> V29LaunchControls:
        required = tuple(self.required_evidence_for_paper_start)
        if required != PAPER_START_REQUIRED_KEYS:
            raise ValueError("V29 paper-start evidence contract was changed")
        not_used = tuple(self.not_used_external_keys_for_candidate_scope)
        if not_used != V29_PRICE_ONLY_NOT_USED_KEYS:
            raise ValueError("V29 price-only scope exclusions were changed")
        return self


class V29StrategySpec(BaseModel):
    """Immutable V29 paper-trading contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_id: str = Field(min_length=1)
    candidate: str = Field(min_length=1)
    version: str = Field(min_length=1)
    stage: Literal["paper"] = "paper"
    market: Market = Market.A_SHARES
    production_ready: Literal[False] = False
    inputs: V29InputRefs
    parameters: V29CandidateParameters
    metrics: V29ResearchMetrics
    controls: V29LaunchControls

    @classmethod
    def from_yaml(cls, path: str | Path) -> V29StrategySpec:
        with Path(path).open("r", encoding="utf-8") as stream:
            document = yaml.safe_load(stream)
        return cls.model_validate(document)

    @model_validator(mode="after")
    def validate_candidate_scope(self) -> V29StrategySpec:
        if self.strategy_id != self.candidate:
            raise ValueError("strategy_id must match candidate")
        if not self.candidate.startswith("v29_price_meta_") or "alt" in self.candidate.lower():
            raise ValueError("V29 production candidate must be price-only and non-alt")
        if self.market != Market.A_SHARES:
            raise ValueError("V29 candidate is currently validated only for A-shares")
        return self
