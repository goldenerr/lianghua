import json
from datetime import datetime, timezone

import httpx
import pytest
from quant_trading.config.loader import ConfigLoader
from quant_trading.config.settings import AccountConfig, Environment, Market, QuantSettings
from quant_trading.core.audit import AuditBus
from quant_trading.execution.order_manager import Order, OrderSide
from quant_trading.integrations import (
    HttpApprovalServiceValidator,
    HttpBorrowAvailabilityProvider,
    HttpExchangePositionProvider,
    HttpSecretManagerResolver,
    HttpServiceConfig,
    ProductionIntegrationError,
)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=1.0)


def test_secret_manager_resolver_fetches_credentials_without_auditing_secret() -> None:
    audit = AuditBus()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/secrets/resolve"
        assert request.headers["authorization"] == "Bearer service-token"
        return httpx.Response(
            200,
            json={
                "exchange": "binance",
                "api_key": "live-key",
                "api_secret": "live-secret",
                "passphrase": "phrase",
            },
        )

    resolver = HttpSecretManagerResolver(
        HttpServiceConfig("https://secrets.example"),
        token_provider=lambda: "service-token",
        audit_bus=audit,
        client=_client(handler),
    )
    account = AccountConfig(
        account_id="prod-1",
        name="prod",
        exchange="binance",
        environment=Environment.PROD,
        secret_ref="vault://quant/accounts/prod-1/binance",
    )

    credentials = resolver(account)

    assert credentials.api_key.get_secret_value() == "live-key"
    assert credentials.api_secret.get_secret_value() == b"live-secret"
    events = audit.query("secret_manager_resolved")
    assert events[-1]["payload"] == {"account_id": "prod-1", "exchange": "binance"}
    assert "live-secret" not in str(events)
    assert "live-key" not in str(events)


def test_secret_manager_fails_closed_on_wrong_exchange_or_unavailable_service() -> None:
    wrong_exchange = HttpSecretManagerResolver(
        HttpServiceConfig("https://secrets.example", max_retries=0),
        client=_client(
            lambda _: httpx.Response(
                200, json={"exchange": "okx", "api_key": "k", "api_secret": "s"}
            )
        ),
    )
    account = AccountConfig(
        account_id="prod-1",
        name="prod",
        exchange="binance",
        environment=Environment.PROD,
        secret_ref="vault://quant/accounts/prod-1/binance",
    )
    with pytest.raises(ProductionIntegrationError, match="wrong exchange"):
        wrong_exchange(account)

    unavailable = HttpSecretManagerResolver(
        HttpServiceConfig("https://secrets.example", max_retries=1),
        client=_client(lambda _: httpx.Response(503, json={"error": "down"})),
    )
    with pytest.raises(ProductionIntegrationError, match="unavailable"):
        unavailable(account)


def test_approval_service_returns_config_hash_bound_reference() -> None:
    audit = AuditBus()
    settings = QuantSettings(config_hash="abc123")

    def handler(request: httpx.Request) -> httpx.Response:
        document = request.read().decode("utf-8")
        assert "abc123" in document
        return httpx.Response(
            200, json={"status": "approved", "approval_ref": "approval://risk/abc123"}
        )

    validator = HttpApprovalServiceValidator(
        HttpServiceConfig("https://approval.example"),
        requested_by="risk-officer",
        audit_bus=audit,
        client=_client(handler),
    )

    assert validator(settings) == "approval://risk/abc123"
    assert audit.query("approval_service_verified")[-1]["payload"]["config_hash"] == "abc123"


def test_approval_service_rejects_unbound_or_denied_approval() -> None:
    settings = QuantSettings(config_hash="abc123")
    unbound = HttpApprovalServiceValidator(
        HttpServiceConfig("https://approval.example"),
        requested_by="risk",
        client=_client(
            lambda _: httpx.Response(
                200, json={"status": "approved", "approval_ref": "approval://risk/other"}
            )
        ),
    )
    with pytest.raises(ProductionIntegrationError, match="config-hash-bound"):
        unbound(settings)

    denied = HttpApprovalServiceValidator(
        HttpServiceConfig("https://approval.example"),
        requested_by="risk",
        client=_client(
            lambda _: httpx.Response(
                200, json={"status": "denied", "approval_ref": "approval://risk/abc123"}
            )
        ),
    )
    with pytest.raises(ProductionIntegrationError, match="did not approve"):
        denied(settings)


def test_exchange_position_provider_returns_fresh_snapshot_and_audits_metadata_only() -> None:
    audit = AuditBus()
    now = datetime(2026, 5, 29, 9, 30, tzinfo=timezone.utc)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/positions/reconciled"
        assert "600519.SH" in request.read().decode("utf-8")
        return httpx.Response(200, json={"quantity": 200.0, "as_of": now.isoformat()})

    provider = HttpExchangePositionProvider(
        HttpServiceConfig("https://broker.example"),
        account_id="prod-1",
        token_provider=lambda: "broker-token",
        audit_bus=audit,
        client=_client(handler),
    )
    order = Order("cid-1", "600519.SH", OrderSide.SELL, 100, market=Market.A_SHARES)

    snapshot = provider(order)

    assert snapshot.quantity == 200.0
    assert snapshot.as_of == now
    assert audit.query("exchange_position_resolved")[-1]["payload"]["symbol"] == "600519.SH"
    assert "broker-token" not in str(audit.query())


def test_exchange_position_provider_rejects_invalid_snapshot() -> None:
    provider = HttpExchangePositionProvider(
        HttpServiceConfig("https://broker.example"),
        account_id="prod-1",
        client=_client(
            lambda _: httpx.Response(200, json={"quantity": "nan", "as_of": "2026-05-29T09:30:00"})
        ),
    )
    order = Order("cid-1", "600519.SH", OrderSide.SELL, 100, market=Market.A_SHARES)

    with pytest.raises(ProductionIntegrationError, match="finite|timezone"):
        provider(order)


def test_borrow_availability_provider_requires_external_evidence_and_audits() -> None:
    audit = AuditBus()
    now = datetime(2026, 5, 29, 9, 30, tzinfo=timezone.utc)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/borrow/availability"
        assert request.headers["authorization"] == "Bearer broker-token"
        assert "300702.SZ" in request.read().decode("utf-8")
        return httpx.Response(
            200,
            json={
                "symbol": "300702.SZ",
                "available_quantity": 12000,
                "borrow_fee_rate": 0.032,
                "as_of": now.isoformat(),
                "evidence_ref": "borrow-feed://broker-prod/300702.SZ/20260529T093000Z",
            },
        )

    provider = HttpBorrowAvailabilityProvider(
        HttpServiceConfig("https://broker.example"),
        account_id="prod-1",
        token_provider=lambda: "broker-token",
        audit_bus=audit,
        client=_client(handler),
    )

    snapshot = provider("300702.SZ")

    assert snapshot.available_quantity == 12000
    assert snapshot.borrow_fee_rate == 0.032
    assert snapshot.evidence_ref.startswith("borrow-feed://")
    event = audit.query("borrow_availability_resolved")[-1]["payload"]
    assert event["symbol"] == "300702.SZ"
    assert "broker-token" not in str(audit.query())


def test_borrow_availability_provider_fails_closed_on_invalid_snapshot() -> None:
    provider = HttpBorrowAvailabilityProvider(
        HttpServiceConfig("https://broker.example"),
        account_id="prod-1",
        client=_client(
            lambda _: httpx.Response(
                200,
                json={
                    "symbol": "300702.SZ",
                    "available_quantity": -1,
                    "borrow_fee_rate": 0.01,
                    "as_of": "2026-05-29T09:30:00",
                    "evidence_ref": "local://borrow",
                },
            )
        ),
    )

    with pytest.raises(ProductionIntegrationError, match="quantity|timezone|evidence"):
        provider("300702.SZ")


def test_config_loader_accepts_real_service_adapters_for_prod(tmp_path) -> None:
    (tmp_path / "accounts").mkdir()
    (tmp_path / "prod.yaml").write_text(
        "env: prod\nrequire_manual_approval: true\n",
        encoding="utf-8",
    )
    (tmp_path / "api.yaml").write_text(
        """
exchanges:
  binance:
    base_url: https://api.binance.com
    ws_url: wss://stream.binance.com:9443/ws
""",
        encoding="utf-8",
    )
    (tmp_path / "accounts" / "prod_account.yaml").write_text(
        """
account_id: prod-1
name: prod
exchange: binance
environment: prod
secret_ref: vault://quant/accounts/prod-1/binance
enabled: true
""",
        encoding="utf-8",
    )

    def secret_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"exchange": "binance", "api_key": "live-key", "api_secret": "live-secret"},
        )

    def approval_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode("utf-8"))
        config_hash = body["config_hash"]
        return httpx.Response(
            200,
            json={"status": "approved", "approval_ref": f"approval://risk/{config_hash}"},
        )

    secret_resolver = HttpSecretManagerResolver(
        HttpServiceConfig("https://secrets.example"),
        client=_client(secret_handler),
    )
    approval_validator = HttpApprovalServiceValidator(
        HttpServiceConfig("https://approval.example"),
        requested_by="risk",
        client=_client(approval_handler),
    )

    settings = ConfigLoader(
        config_dir=tmp_path,
        secret_resolver=secret_resolver,
        approval_validator=approval_validator,
    ).load(env=Environment.PROD)

    assert settings.accounts[0].credentials is not None
    assert settings.config_hash in settings.config_approval_ref
