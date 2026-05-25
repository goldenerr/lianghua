"""Approval-gated strategy lifecycle management for portfolio-004."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from quant_trading.core.audit import AuditBus

UTC = timezone.utc


class Stage(str, Enum):
    DEVELOPING = "developing"
    INCUBATION = "developing"  # Compatibility alias for the historical name.
    PAPER = "paper"
    SMALL_LIVE = "small_live"
    FULL_LIVE = "full_live"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"


_NEXT_STAGE: dict[Stage, Stage] = {
    Stage.DEVELOPING: Stage.PAPER,
    Stage.PAPER: Stage.SMALL_LIVE,
    Stage.SMALL_LIVE: Stage.FULL_LIVE,
    Stage.FULL_LIVE: Stage.DEPRECATED,
    Stage.DEPRECATED: Stage.ARCHIVED,
}


class StageLimit(BaseModel):
    """Externalized capital and approval constraints for one lifecycle stage."""

    model_config = ConfigDict(frozen=True)
    maximum_capital_pct: float = Field(ge=0, le=1)
    requires_risk_approval: bool = True
    minimum_days_in_previous_stage: int = Field(default=0, ge=0)


class LifecyclePolicy(BaseModel):
    """Configuration-backed gate policy, immutable once loaded."""

    model_config = ConfigDict(frozen=True)
    stages: dict[Stage, StageLimit]

    @classmethod
    def from_yaml(cls, path: str | Path) -> LifecyclePolicy:
        with Path(path).open("r", encoding="utf-8") as stream:
            document = yaml.safe_load(stream)
        return cls.model_validate(document)

    def limit_for(self, stage: Stage) -> StageLimit:
        if stage not in self.stages:
            raise ValueError(f"missing lifecycle policy for stage: {stage.value}")
        return self.stages[stage]


@dataclass(frozen=True)
class TransitionRequest:
    strategy_id: str
    from_stage: Stage
    to_stage: Stage
    evidence: dict[str, Any]
    requested_at: str


@dataclass
class StrategyLifecycle:
    """State machine in which live-stage promotions require recorded approval."""

    strategy_id: str
    policy: LifecyclePolicy
    audit_bus: AuditBus = field(default_factory=AuditBus)
    stage: Stage = Stage.DEVELOPING
    created: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    pending_request: TransitionRequest | None = None
    accepting_signals: bool = True

    @property
    def maximum_capital_pct(self) -> float:
        return self.policy.limit_for(self.stage).maximum_capital_pct

    def request_transition(self, target: Stage, evidence: dict[str, Any] | None = None) -> TransitionRequest:
        if _NEXT_STAGE.get(self.stage) != target:
            raise ValueError(f"invalid lifecycle transition: {self.stage.value} -> {target.value}")
        request = TransitionRequest(
            strategy_id=self.strategy_id,
            from_stage=self.stage,
            to_stage=target,
            evidence=dict(evidence or {}),
            requested_at=datetime.now(UTC).isoformat(),
        )
        self.pending_request = request
        self.audit_bus.record(
            "strategy_transition_requested",
            "strategy_lifecycle",
            {"strategy_id": self.strategy_id, "from": self.stage.value, "to": target.value},
        )
        return request

    def approve_transition(self, approved_by: str, approval_ref: str) -> Stage:
        if self.pending_request is None:
            raise ValueError("no pending transition request")
        if not approved_by.strip() or not approval_ref.strip():
            raise PermissionError("risk approval identity and reference are required")
        request = self.pending_request
        target_limit = self.policy.limit_for(request.to_stage)
        elapsed_days = int(request.evidence.get("days_in_current_stage", 0))
        if elapsed_days < target_limit.minimum_days_in_previous_stage:
            raise PermissionError("minimum validation duration for promotion not satisfied")
        old_stage = self.stage
        self.stage = request.to_stage
        self.pending_request = None
        self.audit_bus.record(
            "strategy_transition_approved",
            "strategy_lifecycle",
            {
                "strategy_id": self.strategy_id,
                "from": old_stage.value,
                "to": self.stage.value,
                "approved_by": approved_by,
                "approval_ref": approval_ref,
            },
        )
        return self.stage

    def promote(self) -> bool:
        """Reject historical unapproved promotion calls instead of trading silently."""

        raise PermissionError("direct promotion disabled; request and approve a transition")

    def deprecate(self, approved_by: str, approval_ref: str) -> None:
        if self.stage not in {Stage.PAPER, Stage.SMALL_LIVE, Stage.FULL_LIVE}:
            raise ValueError("only active strategies may be deprecated")
        if not approved_by.strip() or not approval_ref.strip():
            raise PermissionError("risk approval identity and reference are required")
        old_stage = self.stage
        self.accepting_signals = False
        self.stage = Stage.DEPRECATED
        self.audit_bus.record(
            "strategy_deprecated",
            "strategy_lifecycle",
            {
                "strategy_id": self.strategy_id,
                "from": old_stage.value,
                "approved_by": approved_by,
                "approval_ref": approval_ref,
            },
        )

    def archive(self, approval_ref: str) -> None:
        if self.stage != Stage.DEPRECATED or not approval_ref.strip():
            raise PermissionError("deprecated strategy and archive approval reference required")
        self.stage = Stage.ARCHIVED
        self.audit_bus.record(
            "strategy_archived",
            "strategy_lifecycle",
            {"strategy_id": self.strategy_id, "approval_ref": approval_ref},
        )
