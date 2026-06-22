"""Production service adapters for secrets, approvals and exchange positions.

These adapters are thin, fail-closed HTTP clients. They do not store credentials
or place orders; they only resolve approved references and return validated
responses to the existing ConfigLoader and OrderManager injection points.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from pydantic import SecretBytes, SecretStr

from quant_trading.config.settings import AccountConfig, ApiCredentials, QuantSettings
from quant_trading.core.audit import AuditBus
from quant_trading.execution.order_manager import Order, ReconciledPositionSnapshot


class ProductionIntegrationError(RuntimeError):
    """Raised when an external production integration cannot be trusted."""


@dataclass(frozen=True)
class HttpServiceConfig:
    """Shared HTTP safety settings for production integrations."""

    base_url: str
    timeout_seconds: float = 10.0
    max_retries: int = 3

    def __post_init__(self) -> None:
        if not self.base_url.startswith("https://"):
            raise ValueError("production integration base_url must use https")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not 0 <= self.max_retries <= 5:
            raise ValueError("max_retries must be between 0 and 5")


TokenProvider = Callable[[], str]


@dataclass(frozen=True)
class BorrowAvailabilitySnapshot:
    """Broker-backed short/borrow inventory for one symbol."""

    symbol: str
    available_quantity: float
    borrow_fee_rate: float
    as_of: datetime
    evidence_ref: str


class HttpSecretManagerResolver:
    """Resolve production account credentials from an approved Secret Manager."""

    def __init__(
        self,
        config: HttpServiceConfig,
        *,
        token_provider: TokenProvider | None = None,
        audit_bus: AuditBus | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self.token_provider = token_provider
        self.audit = audit_bus or AuditBus()
        self.client = client or httpx.Client(timeout=config.timeout_seconds)

    def __call__(self, account: AccountConfig) -> ApiCredentials:
        if not account.secret_ref:
            raise ProductionIntegrationError("account secret_ref is required")
        payload = {
            "secret_ref": account.secret_ref,
            "account_id": account.account_id,
            "exchange": account.exchange,
        }
        data = self._post_json("/v1/secrets/resolve", payload)
        try:
            credentials = ApiCredentials(
                exchange=str(data["exchange"]),
                api_key=SecretStr(str(data["api_key"])),
                api_secret=SecretBytes(str(data["api_secret"]).encode("utf-8")),
                passphrase=(SecretStr(str(data["passphrase"])) if data.get("passphrase") else None),
                subaccount=str(data["subaccount"]) if data.get("subaccount") else None,
            )
        except Exception as exc:
            raise ProductionIntegrationError("secret manager returned invalid credentials") from exc
        if credentials.exchange != account.exchange:
            raise ProductionIntegrationError(
                "secret manager returned credentials for wrong exchange"
            )
        self.audit.record(
            "secret_manager_resolved",
            "secret_manager",
            {"account_id": account.account_id, "exchange": account.exchange},
        )
        return credentials

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return _post_json_with_retry(
            self.client,
            f"{self.config.base_url.rstrip('/')}{path}",
            payload,
            token_provider=self.token_provider,
            max_retries=self.config.max_retries,
        )


class HttpApprovalServiceValidator:
    """Validate production configuration approval against an external service."""

    def __init__(
        self,
        config: HttpServiceConfig,
        *,
        requested_by: str,
        token_provider: TokenProvider | None = None,
        audit_bus: AuditBus | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not requested_by.strip():
            raise ValueError("requested_by must not be empty")
        self.config = config
        self.requested_by = requested_by
        self.token_provider = token_provider
        self.audit = audit_bus or AuditBus()
        self.client = client or httpx.Client(timeout=config.timeout_seconds)

    def __call__(self, settings: QuantSettings) -> str:
        payload = {
            "config_hash": settings.config_hash,
            "environment": settings.system.env.value,
            "active_markets": [market.value for market in settings.system.primary_markets],
            "requested_by": self.requested_by,
        }
        data = self._post_json("/v1/approvals/configuration", payload)
        approval_ref = str(data.get("approval_ref", "")).strip()
        if not approval_ref or settings.config_hash not in approval_ref:
            raise ProductionIntegrationError(
                "approval service response must include config-hash-bound approval_ref"
            )
        if str(data.get("status", "")).lower() not in {"approved", "accepted"}:
            raise ProductionIntegrationError("approval service did not approve configuration")
        self.audit.record(
            "approval_service_verified",
            "approval_service",
            {"config_hash": settings.config_hash, "approval_ref": approval_ref},
        )
        return approval_ref

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return _post_json_with_retry(
            self.client,
            f"{self.config.base_url.rstrip('/')}{path}",
            payload,
            token_provider=self.token_provider,
            max_retries=self.config.max_retries,
        )


class HttpExchangePositionProvider:
    """Fetch fresh exchange-backed position snapshots for reduce-only checks."""

    def __init__(
        self,
        config: HttpServiceConfig,
        *,
        account_id: str,
        token_provider: TokenProvider | None = None,
        audit_bus: AuditBus | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not account_id.strip():
            raise ValueError("account_id must not be empty")
        self.config = config
        self.account_id = account_id
        self.token_provider = token_provider
        self.audit = audit_bus or AuditBus()
        self.client = client or httpx.Client(timeout=config.timeout_seconds)

    def __call__(self, order: Order) -> ReconciledPositionSnapshot:
        payload = {
            "account_id": self.account_id,
            "symbol": order.symbol,
            "market": order.market.value if order.market is not None else "",
            "client_order_id": order.client_order_id,
        }
        data = self._post_json("/v1/positions/reconciled", payload)
        try:
            quantity = float(data["quantity"])
            as_of = datetime.fromisoformat(str(data["as_of"]))
        except Exception as exc:
            raise ProductionIntegrationError("position provider returned invalid snapshot") from exc
        if not math.isfinite(quantity):
            raise ProductionIntegrationError("position provider quantity must be finite")
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ProductionIntegrationError("position provider timestamp must be timezone-aware")
        self.audit.record(
            "exchange_position_resolved",
            "exchange_position_provider",
            {"account_id": self.account_id, "symbol": order.symbol, "market": payload["market"]},
        )
        return ReconciledPositionSnapshot(quantity=quantity, as_of=as_of)

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return _post_json_with_retry(
            self.client,
            f"{self.config.base_url.rstrip('/')}{path}",
            payload,
            token_provider=self.token_provider,
            max_retries=self.config.max_retries,
        )


class HttpBorrowAvailabilityProvider:
    """Fetch broker-backed borrow availability before any short/market-neutral sleeve."""

    def __init__(
        self,
        config: HttpServiceConfig,
        *,
        account_id: str,
        token_provider: TokenProvider | None = None,
        audit_bus: AuditBus | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not account_id.strip():
            raise ValueError("account_id must not be empty")
        self.config = config
        self.account_id = account_id
        self.token_provider = token_provider
        self.audit = audit_bus or AuditBus()
        self.client = client or httpx.Client(timeout=config.timeout_seconds)

    def __call__(self, symbol: str) -> BorrowAvailabilitySnapshot:
        clean_symbol = symbol.strip()
        if not clean_symbol:
            raise ValueError("symbol must not be empty")
        payload = {"account_id": self.account_id, "symbol": clean_symbol}
        data = self._post_json("/v1/borrow/availability", payload)
        try:
            available_quantity = float(data["available_quantity"])
            borrow_fee_rate = float(data["borrow_fee_rate"])
            as_of = datetime.fromisoformat(str(data["as_of"]))
            evidence_ref = str(data["evidence_ref"]).strip()
        except Exception as exc:
            raise ProductionIntegrationError("borrow provider returned invalid snapshot") from exc
        if str(data.get("symbol", clean_symbol)).strip() != clean_symbol:
            raise ProductionIntegrationError("borrow provider returned availability for wrong symbol")
        if not math.isfinite(available_quantity) or available_quantity < 0:
            raise ProductionIntegrationError("borrow availability quantity must be finite and non-negative")
        if not math.isfinite(borrow_fee_rate) or borrow_fee_rate < 0:
            raise ProductionIntegrationError("borrow fee rate must be finite and non-negative")
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ProductionIntegrationError("borrow provider timestamp must be timezone-aware")
        if not evidence_ref.startswith(("broker://", "borrow-feed://", "exchange://")):
            raise ProductionIntegrationError("borrow provider evidence_ref must be externally verifiable")
        self.audit.record(
            "borrow_availability_resolved",
            "borrow_availability_provider",
            {
                "account_id": self.account_id,
                "symbol": clean_symbol,
                "available_quantity": available_quantity,
                "evidence_ref": evidence_ref,
            },
        )
        return BorrowAvailabilitySnapshot(
            symbol=clean_symbol,
            available_quantity=available_quantity,
            borrow_fee_rate=borrow_fee_rate,
            as_of=as_of,
            evidence_ref=evidence_ref,
        )

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return _post_json_with_retry(
            self.client,
            f"{self.config.base_url.rstrip('/')}{path}",
            payload,
            token_provider=self.token_provider,
            max_retries=self.config.max_retries,
        )


def _post_json_with_retry(
    client: httpx.Client,
    url: str,
    payload: dict[str, Any],
    *,
    token_provider: TokenProvider | None,
    max_retries: int,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token_provider is not None:
        token = token_provider().strip()
        if not token:
            raise ProductionIntegrationError("integration token provider returned an empty token")
        headers["Authorization"] = f"Bearer {token}"

    attempts = max_retries + 1
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            response = client.post(url, json=payload, headers=headers)
            if response.status_code >= 500:
                last_error = ProductionIntegrationError(
                    f"external service temporary failure: {response.status_code}"
                )
                continue
            if response.status_code >= 400:
                raise ProductionIntegrationError(
                    f"external service rejected request: {response.status_code}"
                )
            data = response.json()
            if not isinstance(data, dict):
                raise ProductionIntegrationError("external service response must be an object")
            return data
        except httpx.HTTPError as exc:
            last_error = exc
            continue
    raise ProductionIntegrationError("external service unavailable after retries") from last_error
