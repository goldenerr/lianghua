from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from quant_trading.strategy.v29_spec import (
    PAPER_START_REQUIRED_KEYS,
    V29StrategySpec,
)

PROJECT = Path(__file__).resolve().parents[2]
SPEC_PATH = PROJECT / "config" / "strategies" / "v29_price_meta_longhorizon_guard.paper.yaml"


def _spec_document() -> dict:
    with SPEC_PATH.open("r", encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def test_v29_strategy_spec_loads_fail_closed_paper_contract() -> None:
    spec = V29StrategySpec.from_yaml(SPEC_PATH)

    assert spec.strategy_id == "v29_price_meta_longhorizon_guard"
    assert spec.stage == "paper"
    assert spec.production_ready is False
    assert spec.controls.auto_trade_enabled is False
    assert spec.controls.live_order_submission_allowed is False
    assert spec.controls.max_live_capital_fraction == 0.0
    assert spec.controls.required_evidence_for_paper_start == PAPER_START_REQUIRED_KEYS
    assert spec.metrics.full_sharpe >= 2.0
    assert "price_lowvol_reversal" in spec.parameters.sleeves


def test_v29_strategy_spec_rejects_live_order_enablement() -> None:
    document = _spec_document()
    document["controls"]["live_order_submission_allowed"] = True

    with pytest.raises(ValidationError, match="live_order_submission_allowed"):
        V29StrategySpec.model_validate(document)


def test_v29_strategy_spec_rejects_evidence_contract_changes() -> None:
    document = _spec_document()
    document["controls"]["required_evidence_for_paper_start"] = [
        "external_worm_archive",
        "secret_manager",
    ]

    with pytest.raises(ValidationError, match="evidence contract"):
        V29StrategySpec.model_validate(document)


def test_v29_strategy_spec_rejects_alt_candidate_scope() -> None:
    document = _spec_document()
    document["strategy_id"] = "v29_alt_meta_research_only"
    document["candidate"] = "v29_alt_meta_research_only"

    with pytest.raises(ValidationError, match="price-only"):
        V29StrategySpec.model_validate(document)
