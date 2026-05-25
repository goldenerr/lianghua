import pytest
from quant_trading.factor_store import FactorDefinition, FactorStore


def test_factor_versions_are_immutable_and_require_adjusted_data() -> None:
    store = FactorStore()
    store.register(FactorDefinition("momentum", version="v1"))
    with pytest.raises(ValueError, match="immutable"):
        store.register(FactorDefinition("momentum", dtype="float32", version="v1"))
    with pytest.raises(ValueError, match="unadjusted"):
        FactorDefinition("bad", corporate_action_adjusted=False)


def test_offline_online_hash_verifies_feature_parity() -> None:
    store = FactorStore()
    store.register(FactorDefinition("volatility", version="v2"))
    snapshot = {"AAPL": 0.12, "MSFT": 0.08}
    store.publish_offline_values("volatility", "v2", snapshot)
    store.publish_online_values("volatility", "v2", snapshot)
    assert store.verify_offline_online_parity("volatility", "v2") is True
    store.publish_online_values("volatility", "v2", {"AAPL": 0.15, "MSFT": 0.08})
    assert store.verify_offline_online_parity("volatility", "v2") is False
