import numpy as np
from quant_trading.strategy.adaptive_weights import AdaptiveWeights, rolling_spearman_ic
from quant_trading.strategy.gpu_factors import compute_factor_matrix_cpu, compute_factor_matrix_gpu
from quant_trading.strategy.ml_model import (
    FactorTimingModel,
    MLCConfig,
    build_ic_prediction_features,
    compute_rolling_ic,
    detect_factor_crowding,
    detect_market_regime,
)


def test_ic_features_regimes_and_adaptive_weights() -> None:
    rng = np.random.default_rng(4)
    factor = rng.normal(size=(30, 150))
    returns = factor * 0.03 + rng.normal(scale=0.01, size=(30, 150))
    ic = compute_rolling_ic(factor, returns)
    regimes = detect_market_regime(rng.normal(0.0002, 0.01, 150))
    features, targets = build_ic_prediction_features(
        ic,
        {"quality": ic * 0.5},
        ["momentum", "quality"],
        regimes,
        lookback=60,
    )
    assert np.nanmean(ic[10:]) > 0.8
    assert features.shape[0] == targets.shape[0] > 0

    values = {"momentum": factor[:, -1], "noise": rng.normal(size=30)}
    correlations = rolling_spearman_ic(values, returns[:, -1])
    weights = AdaptiveWeights({"momentum": 0.5, "noise": 0.5}).update(values, returns[:, -1])
    assert correlations["momentum"] > correlations["noise"]
    assert np.isclose(sum(weights.values()), 1.0)
    assert weights["momentum"] > weights["noise"]


def test_linear_fallback_predicts_ic_and_records_training_day(monkeypatch) -> None:
    rng = np.random.default_rng(7)
    features = rng.normal(size=(40, 3))
    targets = 0.5 * features[:, 0] - 0.25 * features[:, 1]
    model = FactorTimingModel(MLCConfig(min_training_samples=5))
    monkeypatch.setattr(model, "_get_xgb", lambda: None)

    model.train(features, targets, factor_name="momentum", train_day=125)
    predictions = model.predict_ic(features[:5], "momentum")
    dynamic = model.compute_dynamic_weights(
        {"momentum": features[:1], "value": features[:1]},
        ["momentum", "value"],
        {"momentum": 0.5, "value": 0.5},
    )
    assert model.last_train_day == 125
    assert not np.allclose(predictions, 0)
    assert np.isclose(sum(dynamic.values()), 1.0)


def test_crowding_and_cpu_gpu_fallback_factor_matrix(monkeypatch) -> None:
    rng = np.random.default_rng(11)
    closes = 100 + rng.normal(0.05, 0.5, size=(3, 280)).cumsum(axis=1)
    volumes = rng.uniform(500_000, 1_500_000, size=(3, 280))
    amounts = closes * volumes
    cpu = compute_factor_matrix_cpu(closes, volumes, amounts)

    import quant_trading.strategy.gpu_factors as gpu
    monkeypatch.setattr(gpu, "HAS_CUPY", False)
    fallback = compute_factor_matrix_gpu(closes, volumes, amounts)
    assert set(cpu) == set(fallback)
    assert np.isfinite(cpu["vol_1m"]).all()
    assert np.isfinite(cpu["amihud_illiq"]).all()

    signal = detect_factor_crowding({"momentum": rng.normal(size=(30, 80))})
    assert 0 <= signal["momentum"] <= 1
