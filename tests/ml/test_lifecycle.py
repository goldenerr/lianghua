import numpy as np
import pytest
from quant_trading.core.audit import AuditBus
from quant_trading.ml.lifecycle import (
    ModelPromotionGate,
    ModelStage,
    ModelVersion,
    detect_feature_drift,
    evaluate_adversarial_robustness,
    evaluate_regime_bias,
    shadow_test,
)
from quant_trading.ml.online import ModelRollbackController, ShadowDeployment, detect_concept_drift


def test_psi_detects_shifted_feature_distribution() -> None:
    baseline = np.linspace(0.0, 1.0, 100)
    shifted = np.linspace(2.0, 3.0, 100)
    report = detect_feature_drift(baseline, shifted, feature_names=["momentum"])
    assert report["drifted"] is True
    assert report["psi_by_feature"]["momentum"] > 0.2


def test_robustness_and_regime_bias_provide_production_gates() -> None:
    predictor = lambda values: values[:, 0] * 0.1
    robustness = evaluate_adversarial_robustness(predictor, np.ones((20, 1)), tolerance=0.001)
    bias = evaluate_regime_bias([0, 0, 1, 1], [0, 0.1, 0, 0], ["bull", "bull", "bear", "bear"], 0.2)
    assert robustness["passed"] is True
    assert bias["biased"] is True


def test_shadow_test_requires_aligned_predictions() -> None:
    report = shadow_test(lambda x: x[:, 0], lambda x: x[:, 0] + 0.01, np.ones((3, 1)))
    assert report["mean_absolute_delta"] == pytest.approx(0.01)
    with pytest.raises(ValueError, match="aligned"):
        shadow_test(lambda x: np.array([1]), lambda x: np.array([1]), np.ones((3, 1)))


def test_model_production_promotion_requires_signature_duration_and_approval() -> None:
    audit = AuditBus()
    gate = ModelPromotionGate(audit, minimum_paper_days=90)
    model = ModelVersion("signal", "v2", ModelStage.PAPER, signature_verified=True)
    with pytest.raises(PermissionError, match="duration"):
        gate.promote_to_production(model, 89, "risk", "APR-1")
    promoted = gate.promote_to_production(model, 90, "risk", "APR-2")
    assert promoted.stage == ModelStage.PROD
    assert audit.query("model_production_approved")


def test_online_drift_automatically_rolls_active_candidate_back() -> None:
    audit = AuditBus()
    controller = ModelRollbackController(ShadowDeployment("stable-v1"), audit)
    controller.register_candidate("candidate-v2")
    controller.approve_candidate("risk", "APR-ML")
    drift = detect_concept_drift(np.zeros(50), np.ones(50), window=50)
    assert controller.rollback_on_drift(drift) is True
    assert controller.deployment.active_version == "stable-v1"
    assert audit.verify_integrity()
