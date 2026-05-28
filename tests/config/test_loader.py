"""Tests for ConfigLoader — layered loading, hot-reload, Vault, env vars."""

from pathlib import Path

import pytest
import yaml
from pydantic import SecretBytes, SecretStr
from quant_trading.config.loader import (
    ConfigLoader,
    get_global_loader,
    install_hotreload_handler,
    load_config,
)
from quant_trading.config.market_calendar import CalendarUnavailableError
from quant_trading.config.market_router import MarketRouter
from quant_trading.config.settings import ApiCredentials, Environment, Market
from quant_trading.core.audit import AuditBus

# ── Helpers ───────────────────────────────────────────────────────────────────


def _write_yaml(path: Path, data: dict) -> None:
    """Write a dict as YAML file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(data, f)


# ── ConfigLoader Tests ────────────────────────────────────────────────────────


class TestConfigLoader:
    """Test layered config loading."""

    def test_loads_default_config(self, tmp_path: Path):
        """ConfigLoader should load defaults when no files exist."""
        loader = ConfigLoader(config_dir=tmp_path)
        settings = loader.load()

        assert settings.system.env == Environment.DEV
        assert settings.system.primary_markets == ("A股",)
        assert settings.risk.max_position_pct == 0.20
        assert settings.accounts == ()

    def test_loads_partial_config(self, tmp_path: Path):
        """Should load partial config — only risk.yaml present."""
        _write_yaml(
            tmp_path / "risk.yaml",
            {
                "max_position_pct": 0.15,
                "max_total_leverage": 3.0,
            },
        )

        loader = ConfigLoader(config_dir=tmp_path)
        settings = loader.load()

        assert settings.risk.max_position_pct == 0.15
        assert settings.risk.max_total_leverage == 3.0
        assert settings.system.primary_markets == ("A股",)  # default

    def test_loads_full_config(self, tmp_path: Path):
        """Should load system, risk, api, and account configs."""
        _write_yaml(
            tmp_path / "system.yaml",
            {
                "env": "dev",
                "primary_markets": ["A股", "加密货币"],
                "trading_sessions": [
                    {
                        "market": "A股",
                        "sessions": [["09:30", "11:30"], ["13:00", "15:00"]],
                        "timezone": "Asia/Shanghai",
                        "holiday_calendar": "XSHG",
                    },
                    {
                        "market": "加密货币",
                        "sessions": [["00:00", "24:00"]],
                        "timezone": "UTC",
                    },
                ],
                "market_rules": [
                    {
                        "market": "A股",
                        "tick_size": 0.01,
                        "lot_size": 100,
                        "price_precision": 2,
                        "data_sources": ["tushare", "akshare"],
                        "fee_model": "cn_stock",
                    },
                    {
                        "market": "加密货币",
                        "tick_size": 0.01,
                        "lot_size": 1,
                        "price_precision": 2,
                        "data_sources": ["ccxt"],
                        "fee_model": "crypto_maker_taker",
                    },
                ],
            },
        )
        _write_yaml(
            tmp_path / "risk.yaml",
            {
                "max_position_pct": 0.10,
            },
        )
        _write_yaml(
            tmp_path / "api.yaml",
            {
                "exchanges": {
                    "binance": {
                        "base_url": "https://api.binance.com",
                        "timeout_seconds": 5,
                        "max_retries": 2,
                        "rate_limit_rps": 10,
                        "circuit_breaker_failures": 5,
                        "circuit_breaker_cooldown_seconds": 30,
                    }
                }
            },
        )
        _write_yaml(
            tmp_path / "accounts" / "dev.yaml",
            {
                "account_id": "dev-001",
                "name": "Dev",
                "exchange": "binance",
                "credentials": {
                    "exchange": "binance",
                    "api_key": "k",
                    "api_secret": "s",
                },
            },
        )

        loader = ConfigLoader(config_dir=tmp_path)
        settings = loader.load()

        assert len(settings.system.primary_markets) == 2
        assert settings.risk.max_position_pct == 0.10
        assert "binance" in settings.api.exchanges
        assert settings.api.exchanges["binance"].timeout_seconds == 5
        assert len(settings.accounts) == 1
        assert settings.accounts[0].account_id == "dev-001"

    def test_loaded_hash_bound_non_market_snapshot_is_immutable(self, tmp_path: Path):
        _write_yaml(
            tmp_path / "risk.yaml",
            {
                "strategy": {
                    "top_n": 2,
                    "rebalance_freq_days": 5,
                    "factors": ["momentum", "volatility"],
                    "weights": {"momentum": 0.6, "volatility": 0.4},
                    "sector_cap": 1,
                }
            },
        )
        _write_yaml(
            tmp_path / "api.yaml",
            {"exchanges": {"binance": {"base_url": "https://api.binance.com"}}},
        )
        _write_yaml(
            tmp_path / "accounts" / "dev.yaml",
            {
                "account_id": "dev-001",
                "name": "Dev",
                "exchange": "binance",
                "credentials": {"exchange": "binance", "api_key": "k", "api_secret": "s"},
            },
        )

        settings = ConfigLoader(config_dir=tmp_path).load()
        assert settings.config_hash
        with pytest.raises(TypeError):
            settings.risk.strategy.weights["momentum"] = 1.0  # type: ignore[union-attr,index]
        with pytest.raises(TypeError):
            settings.api.exchanges["forged"] = settings.api.exchanges["binance"]  # type: ignore[index]
        with pytest.raises(AttributeError):
            settings.accounts.append(settings.accounts[0])  # type: ignore[attr-defined]

    def test_published_snapshot_is_audited_without_secret_material(self, tmp_path: Path):
        _write_yaml(
            tmp_path / "accounts" / "dev.yaml",
            {
                "account_id": "dev-001",
                "name": "Dev",
                "exchange": "binance",
                "credentials": {
                    "exchange": "binance",
                    "api_key": "VERY_SECRET_API_KEY",
                    "api_secret": "VERY_SECRET_API_SECRET",
                },
            },
        )
        audit = AuditBus()
        settings = ConfigLoader(config_dir=tmp_path, audit_bus=audit).load()

        payload = audit.query("configuration_snapshot_authorized_for_publish")[-1]["payload"]
        assert payload["config_hash"] == settings.config_hash
        assert payload["account_count"] == 1
        assert payload["approval_present"] is False
        assert "VERY_SECRET" not in repr(payload)
        assert audit.query("configuration_snapshot_published")[-1]["payload"] == payload

    def test_audit_failure_prevents_runtime_snapshot_publication(self, tmp_path: Path):
        baseline = ConfigLoader(config_dir=tmp_path).load()

        class FailingAuditBus(AuditBus):
            def record(self, event_type: str, source: str, payload: dict, **kwargs: object) -> None:
                del event_type, source, payload, kwargs
                raise RuntimeError("audit unavailable")

        _write_yaml(
            tmp_path / "system.yaml",
            {
                "primary_markets": ["加密货币"],
                "trading_sessions": [
                    {"market": "加密货币", "sessions": [["00:00", "24:00"]], "timezone": "UTC"}
                ],
                "market_rules": [
                    {
                        "market": "加密货币",
                        "tick_size": 0.01,
                        "lot_size": 1,
                        "price_precision": 2,
                        "data_sources": ["ccxt"],
                        "fee_model": "crypto_maker_taker",
                    }
                ],
            },
        )
        with pytest.raises(RuntimeError, match="audit unavailable"):
            ConfigLoader(config_dir=tmp_path, audit_bus=FailingAuditBus()).load()
        assert baseline.system.primary_markets == (Market.A_SHARES,)
        assert MarketRouter.get_fee_model(Market.A_SHARES) == "cn_stock"
        with pytest.raises(ValueError, match="primary_markets"):
            MarketRouter.get_fee_model(Market.CRYPTO)

    def test_publish_commit_audit_failure_preserves_runtime_snapshot(self, tmp_path: Path):
        baseline = ConfigLoader(config_dir=tmp_path).load()

        class FailOnCommitAuditBus(AuditBus):
            def record(self, event_type: str, source: str, payload: dict, **kwargs: object) -> None:
                if event_type == "configuration_snapshot_published":
                    raise RuntimeError("commit audit unavailable")
                super().record(event_type, source, payload, **kwargs)

        _write_yaml(
            tmp_path / "system.yaml",
            {
                "primary_markets": ["加密货币"],
                "trading_sessions": [
                    {"market": "加密货币", "sessions": [["00:00", "24:00"]], "timezone": "UTC"}
                ],
                "market_rules": [
                    {
                        "market": "加密货币",
                        "tick_size": 0.01,
                        "lot_size": 1,
                        "price_precision": 2,
                        "data_sources": ["ccxt"],
                        "fee_model": "crypto_maker_taker",
                    }
                ],
            },
        )
        with pytest.raises(RuntimeError, match="commit audit unavailable"):
            ConfigLoader(config_dir=tmp_path, audit_bus=FailOnCommitAuditBus()).load()
        assert baseline.system.primary_markets == (Market.A_SHARES,)
        assert MarketRouter.get_fee_model(Market.A_SHARES) == "cn_stock"
        with pytest.raises(ValueError, match="primary_markets"):
            MarketRouter.get_fee_model(Market.CRYPTO)

    def test_direct_load_synchronizes_cached_settings_and_runtime_snapshot(self, tmp_path: Path):
        audit = AuditBus()
        loader = ConfigLoader(config_dir=tmp_path, audit_bus=audit)
        initial = loader.load()
        assert loader.settings is initial

        _write_yaml(
            tmp_path / "system.yaml",
            {
                "primary_markets": ["加密货币"],
                "trading_sessions": [
                    {"market": "加密货币", "sessions": [["00:00", "24:00"]], "timezone": "UTC"}
                ],
                "market_rules": [
                    {
                        "market": "加密货币",
                        "tick_size": 0.01,
                        "lot_size": 1,
                        "price_precision": 2,
                        "data_sources": ["ccxt"],
                        "fee_model": "crypto_maker_taker",
                    }
                ],
            },
        )
        activated = loader.load()
        assert activated.config_hash != initial.config_hash
        assert loader.settings is activated
        assert MarketRouter.get_fee_model(Market.CRYPTO) == "crypto_maker_taker"
        with pytest.raises(ValueError, match="primary_markets"):
            MarketRouter.get_fee_model(Market.A_SHARES)
        payload = audit.query("configuration_reloaded")[-1]["payload"]
        assert payload["previous_config_hash"] == initial.config_hash
        assert payload["new_config_hash"] == activated.config_hash

    def test_env_override_merges(self, tmp_path: Path):
        """Environment file should override base values."""
        _write_yaml(
            tmp_path / "system.yaml",
            {
                "env": "dev",
                "auto_trade_enabled": False,
                "warmup_bars": 20,
            },
        )
        _write_yaml(
            tmp_path / "dev.yaml",
            {
                "warmup_bars": 40,
            },
        )

        loader = ConfigLoader(config_dir=tmp_path)
        settings = loader.load()

        # dev.yaml should override a validated non-funds-impacting setting.
        assert settings.system.warmup_bars == 40
        # Safety-sensitive value from system.yaml is preserved.
        assert settings.system.auto_trade_enabled is False

    def test_detects_env_from_files(self, tmp_path: Path):
        """Should detect environment from existing env files."""
        _write_yaml(tmp_path / "system.yaml", {"env": "dev"})
        _write_yaml(tmp_path / "dev.yaml", {"env": "dev"})

        loader = ConfigLoader(config_dir=tmp_path)
        settings = loader.load()
        assert settings.system.env == Environment.DEV

    def test_falls_back_to_dev(self, tmp_path: Path):
        """Should return DEV when no env files exist."""
        _write_yaml(tmp_path / "system.yaml", {"env": "dev"})

        loader = ConfigLoader(config_dir=tmp_path)
        settings = loader.load()
        assert settings.system.env == Environment.DEV

    def test_explicit_env(self, tmp_path: Path):
        """Should use explicitly provided environment."""
        _write_yaml(tmp_path / "system.yaml", {"env": "dev"})
        _write_yaml(tmp_path / "test.yaml", {"env": "test"})

        loader = ConfigLoader(config_dir=tmp_path)
        settings = loader.load(env=Environment.TEST)
        assert settings.system.env == Environment.TEST

    def test_reload_atomically_replaces_cached_settings(self, tmp_path: Path):
        """Successful reload replaces the cached validated settings snapshot."""
        _write_yaml(tmp_path / "system.yaml", {"primary_markets": ["A股"]})

        audit = AuditBus()
        loader = ConfigLoader(config_dir=tmp_path, audit_bus=audit)
        s1 = loader.settings
        assert s1.system.primary_markets == ("A股",)

        # Modify file on disk
        _write_yaml(
            tmp_path / "system.yaml",
            {
                "primary_markets": ["加密货币"],
                "trading_sessions": [
                    {
                        "market": "加密货币",
                        "sessions": [["00:00", "24:00"]],
                        "timezone": "UTC",
                    }
                ],
                "market_rules": [
                    {
                        "market": "加密货币",
                        "tick_size": 0.01,
                        "lot_size": 1,
                        "price_precision": 2,
                        "data_sources": ["ccxt"],
                        "fee_model": "crypto_maker_taker",
                    }
                ],
            },
        )

        s2 = loader.reload()
        assert s2.system.primary_markets == ("加密货币",)
        assert loader.settings is s2
        payload = audit.query("configuration_reloaded")[-1]["payload"]
        assert payload["previous_config_hash"] == s1.config_hash
        assert payload["new_config_hash"] == s2.config_hash
        assert payload["changed"] is True

    def test_reload_audit_failure_preserves_verified_settings_and_market_controls(
        self, tmp_path: Path
    ):
        class FailReloadAuditBus(AuditBus):
            def record(self, event_type: str, source: str, payload: dict, **kwargs: object) -> None:
                if event_type == "configuration_reloaded":
                    raise RuntimeError("reload audit unavailable")
                super().record(event_type, source, payload, **kwargs)

        audit = FailReloadAuditBus()
        loader = ConfigLoader(config_dir=tmp_path, audit_bus=audit)
        verified = loader.settings
        _write_yaml(
            tmp_path / "system.yaml",
            {
                "primary_markets": ["加密货币"],
                "trading_sessions": [
                    {"market": "加密货币", "sessions": [["00:00", "24:00"]], "timezone": "UTC"}
                ],
                "market_rules": [
                    {
                        "market": "加密货币",
                        "tick_size": 0.01,
                        "lot_size": 1,
                        "price_precision": 2,
                        "data_sources": ["ccxt"],
                        "fee_model": "crypto_maker_taker",
                    }
                ],
            },
        )

        with pytest.raises(RuntimeError, match="reload audit unavailable"):
            loader.reload()
        assert loader.settings is verified
        assert MarketRouter.get_fee_model(Market.A_SHARES) == "cn_stock"
        with pytest.raises(ValueError, match="primary_markets"):
            MarketRouter.get_fee_model(Market.CRYPTO)
        assert audit.query("configuration_reload_rejected")[-1]["payload"]["error_type"] == (
            "RuntimeError"
        )

    def test_failed_reload_preserves_verified_settings_and_market_controls(self, tmp_path: Path):
        audit = AuditBus()
        loader = ConfigLoader(config_dir=tmp_path, audit_bus=audit)
        verified = loader.settings
        assert MarketRouter.get_fee_model(Market.A_SHARES) == "cn_stock"
        _write_yaml(
            tmp_path / "system.yaml",
            {
                "primary_markets": ["期权"],
                "trading_sessions": [
                    {
                        "market": "期权",
                        "sessions": [["09:30", "11:30"]],
                        "timezone": "Asia/Shanghai",
                        "holiday_calendar": "UNKNOWN_CALENDAR",
                    }
                ],
                "market_rules": [
                    {
                        "market": "期权",
                        "tick_size": 0.01,
                        "lot_size": 1,
                        "price_precision": 2,
                        "data_sources": ["candidate_provider"],
                        "fee_model": "candidate_fee",
                    }
                ],
            },
        )

        with pytest.raises(CalendarUnavailableError, match="cannot be initialized"):
            loader.reload()
        assert loader.settings is verified
        assert MarketRouter.get_fee_model(Market.A_SHARES) == "cn_stock"
        with pytest.raises(ValueError, match="primary_markets"):
            MarketRouter.get_fee_model(Market.OPTIONS)
        payload = audit.query("configuration_reload_rejected")[-1]["payload"]
        assert payload["previous_config_hash"] == verified.config_hash
        assert payload["error_type"] == "CalendarUnavailableError"


class TestEnvVarOverrides:
    """Test environment variable secret overrides."""

    def test_env_var_overrides_credentials(self, tmp_path: Path, monkeypatch):
        """QUANT_API_KEY should override all account credentials."""
        _write_yaml(tmp_path / "system.yaml", {"env": "dev"})
        _write_yaml(
            tmp_path / "accounts" / "dev.yaml",
            {
                "account_id": "dev-001",
                "name": "Dev",
                "exchange": "binance",
                "credentials": {
                    "exchange": "binance",
                    "api_key": "placeholder",
                    "api_secret": "placeholder",
                },
            },
        )

        monkeypatch.setenv("QUANT_API_KEY", "real_key_from_env")
        monkeypatch.setenv("QUANT_API_SECRET", "real_secret_from_env")

        loader = ConfigLoader(config_dir=tmp_path)
        settings = loader.load()

        assert settings.accounts[0].credentials.api_key.get_secret_value() == "real_key_from_env"
        assert (
            settings.accounts[0].credentials.api_secret.get_secret_value()
            == b"real_secret_from_env"
        )


class TestProdSecretPolicy:
    """Test strict prod secret policy."""

    @staticmethod
    def _write_prod_reference(tmp_path: Path) -> None:
        _write_yaml(tmp_path / "system.yaml", {"env": "prod", "require_manual_approval": True})
        _write_yaml(
            tmp_path / "api.yaml",
            {
                "exchanges": {
                    "binance": {
                        "base_url": "https://api.binance.com",
                        "ws_url": "wss://stream.binance.com:9443/ws",
                    }
                }
            },
        )
        _write_yaml(
            tmp_path / "accounts" / "prod.yaml",
            {
                "account_id": "prod-001",
                "name": "Prod",
                "exchange": "binance",
                "environment": "prod",
                "secret_ref": "vault://quant/accounts/prod-001/binance",
            },
        )

    def test_prod_without_secret_resolver_fails(self, tmp_path: Path):
        self._write_prod_reference(tmp_path)
        with pytest.raises(ValueError, match="secret_resolver"):
            ConfigLoader(config_dir=tmp_path).load(env=Environment.PROD)

    def test_prod_plaintext_credentials_are_always_rejected(self, tmp_path: Path):
        _write_yaml(tmp_path / "system.yaml", {"env": "prod"})
        _write_yaml(
            tmp_path / "accounts" / "prod.yaml",
            {
                "account_id": "prod-001",
                "name": "Prod",
                "exchange": "binance",
                "environment": "prod",
                "credentials": {
                    "exchange": "binance",
                    "api_key": "k",
                    "api_secret": "s",
                },
            },
        )

        with pytest.raises(ValueError, match="plaintext credentials are prohibited"):
            ConfigLoader(config_dir=tmp_path).load(env=Environment.PROD)

    def test_prod_env_credential_override_is_rejected(self, tmp_path: Path, monkeypatch):
        self._write_prod_reference(tmp_path)
        monkeypatch.setenv("QUANT_API_KEY", "forbidden")

        def resolver(_account: object) -> ApiCredentials:
            return ApiCredentials(
                exchange="binance",
                api_key=SecretStr("vault-key"),
                api_secret=SecretBytes(b"vault-secret"),
            )

        with pytest.raises(ValueError, match="environment-variable credential"):
            ConfigLoader(config_dir=tmp_path, secret_resolver=resolver).load(env=Environment.PROD)

    def test_prod_requires_configuration_approval(self, tmp_path: Path):
        self._write_prod_reference(tmp_path)

        def resolver(_account: object) -> ApiCredentials:
            return ApiCredentials(
                exchange="binance",
                api_key=SecretStr("vault-key"),
                api_secret=SecretBytes(b"vault-secret"),
            )

        with pytest.raises(ValueError, match="approval_validator"):
            ConfigLoader(config_dir=tmp_path, secret_resolver=resolver).load(env=Environment.PROD)

    def test_prod_account_exchange_requires_configured_endpoint(self, tmp_path: Path):
        self._write_prod_reference(tmp_path)
        _write_yaml(tmp_path / "api.yaml", {"exchanges": {}})

        def resolver(_account: object) -> ApiCredentials:
            return ApiCredentials(
                exchange="binance",
                api_key=SecretStr("vault-key"),
                api_secret=SecretBytes(b"vault-secret"),
            )

        audit = AuditBus()
        with pytest.raises(ValueError, match="no configured API endpoint"):
            ConfigLoader(
                config_dir=tmp_path,
                secret_resolver=resolver,
                approval_validator=lambda settings: f"RISK-APPROVAL-{settings.config_hash}",
                audit_bus=audit,
            ).load(env=Environment.PROD)
        assert audit.query("configuration_load_rejected")[-1]["payload"]["error_type"] == (
            "ValueError"
        )

    @pytest.mark.parametrize(
        ("endpoint_override", "message"),
        [
            ({"base_url": "http://api.binance.com"}, "base_url must use HTTPS"),
            (
                {"base_url": "https://user:pass@api.binance.com"},
                "base_url must not contain credentials",
            ),
            (
                {"base_url": "https://api.binance.com", "ws_url": "ws://stream.binance.com/ws"},
                "ws_url must use WSS",
            ),
            (
                {
                    "base_url": "https://api.binance.com",
                    "ws_url": "wss://user:pass@stream.binance.com/ws",
                },
                "ws_url must not contain credentials",
            ),
        ],
    )
    def test_prod_api_endpoints_must_be_tls_and_not_embed_credentials(
        self, tmp_path: Path, endpoint_override: dict[str, str], message: str
    ):
        self._write_prod_reference(tmp_path)
        _write_yaml(tmp_path / "api.yaml", {"exchanges": {"binance": endpoint_override}})

        def resolver(_account: object) -> ApiCredentials:
            return ApiCredentials(
                exchange="binance",
                api_key=SecretStr("vault-key"),
                api_secret=SecretBytes(b"vault-secret"),
            )

        with pytest.raises(ValueError, match=message):
            ConfigLoader(
                config_dir=tmp_path,
                secret_resolver=resolver,
                approval_validator=lambda settings: f"RISK-APPROVAL-{settings.config_hash}",
            ).load(env=Environment.PROD)

    def test_prod_rejects_approval_not_bound_to_full_config_hash(self, tmp_path: Path):
        self._write_prod_reference(tmp_path)

        def resolver(_account: object) -> ApiCredentials:
            return ApiCredentials(
                exchange="binance",
                api_key=SecretStr("vault-key"),
                api_secret=SecretBytes(b"vault-secret"),
            )

        audit = AuditBus()
        with pytest.raises(ValueError, match="full config hash"):
            ConfigLoader(
                config_dir=tmp_path,
                secret_resolver=resolver,
                approval_validator=lambda settings: f"RISK-APPROVAL-{settings.config_hash[:8]}",
                audit_bus=audit,
            ).load(env=Environment.PROD)
        payload = audit.query("configuration_approval_rejected")[-1]["payload"]
        assert payload["reason"] == "config_hash_not_bound"

    def test_prod_uses_resolved_credentials_and_approval(self, tmp_path: Path):
        self._write_prod_reference(tmp_path)

        def resolver(_account: object) -> ApiCredentials:
            return ApiCredentials(
                exchange="binance",
                api_key=SecretStr("vault-key"),
                api_secret=SecretBytes(b"vault-secret"),
            )

        audit = AuditBus()
        settings = ConfigLoader(
            config_dir=tmp_path,
            secret_resolver=resolver,
            approval_validator=lambda settings: f"RISK-APPROVAL-{settings.config_hash}",
            audit_bus=audit,
        ).load(env=Environment.PROD)
        credentials = settings.accounts[0].credentials
        assert credentials is not None
        assert credentials.api_key.get_secret_value() == "vault-key"
        assert credentials.api_secret.get_secret_value() == b"vault-secret"
        assert settings.config_approval_ref.startswith("RISK-APPROVAL-")
        assert settings.config_hash in settings.config_approval_ref
        assert settings.config_hash
        assert audit.query("configuration_approval_verified")[-1]["payload"]["config_hash"] == (
            settings.config_hash
        )


class TestValidation:
    """Test startup validation."""

    def test_duplicate_account_ids_are_rejected_and_audited(self, tmp_path: Path):
        for filename in ("first.yaml", "second.yaml"):
            _write_yaml(
                tmp_path / "accounts" / filename,
                {
                    "account_id": "duplicate-account",
                    "name": filename,
                    "exchange": "binance",
                    "credentials": {
                        "exchange": "binance",
                        "api_key": f"key-{filename}",
                        "api_secret": f"secret-{filename}",
                    },
                },
            )

        audit = AuditBus()
        with pytest.raises(ValueError, match="duplicate account_id"):
            ConfigLoader(config_dir=tmp_path, audit_bus=audit).load()
        assert audit.query("configuration_load_rejected")[-1]["payload"]["error_type"] == (
            "ValueError"
        )

    def test_empty_markets_fails(self, tmp_path: Path):
        """Empty primary_markets should raise ValueError."""
        _write_yaml(tmp_path / "system.yaml", {"primary_markets": []})

        audit = AuditBus()
        loader = ConfigLoader(config_dir=tmp_path, audit_bus=audit)
        with pytest.raises(ValueError, match="primary_markets"):
            loader.load()
        payload = audit.query("configuration_load_rejected")[-1]["payload"]
        assert payload["previous_config_hash"] == ""
        assert payload["error_type"] == "ValidationError"

    def test_unknown_environment_override_fails(self, tmp_path: Path):
        _write_yaml(tmp_path / "system.yaml", {"env": "dev"})
        _write_yaml(tmp_path / "dev.yaml", {"undeclared_limit": 7})

        with pytest.raises(ValueError, match="unknown environment override"):
            ConfigLoader(config_dir=tmp_path).load()

    def test_unregistered_active_market_calendar_blocks_startup(self, tmp_path: Path):
        _write_yaml(
            tmp_path / "system.yaml",
            {
                "primary_markets": ["期权"],
                "trading_sessions": [
                    {
                        "market": "期权",
                        "sessions": [["09:30", "11:30"]],
                        "timezone": "Asia/Shanghai",
                        "holiday_calendar": "UNKNOWN_CALENDAR",
                    }
                ],
                "market_rules": [
                    {
                        "market": "期权",
                        "tick_size": 0.01,
                        "lot_size": 1,
                        "price_precision": 2,
                        "data_sources": ["approved_provider"],
                        "fee_model": "approved_options",
                    }
                ],
            },
        )

        with pytest.raises(CalendarUnavailableError, match="cannot be initialized"):
            ConfigLoader(config_dir=tmp_path).load()

    def test_holiday_calendar_is_bound_into_public_config_hash(self, tmp_path: Path):
        _write_yaml(
            tmp_path / "system.yaml",
            {
                "primary_markets": ["A股"],
                "trading_sessions": [
                    {
                        "market": "A股",
                        "sessions": [["09:30", "11:30"], ["13:00", "15:00"]],
                        "timezone": "Asia/Shanghai",
                        "holiday_calendar": "XSHG",
                    }
                ],
                "market_rules": [
                    {
                        "market": "A股",
                        "tick_size": 0.01,
                        "lot_size": 100,
                        "price_precision": 2,
                        "data_sources": ["tushare"],
                        "fee_model": "cn_stock",
                    }
                ],
            },
        )
        baseline_hash = ConfigLoader(config_dir=tmp_path).load().config_hash
        document = yaml.safe_load((tmp_path / "system.yaml").read_text(encoding="utf-8"))
        document["trading_sessions"][0]["holiday_calendar"] = "NYSE"
        _write_yaml(tmp_path / "system.yaml", document)
        changed_hash = ConfigLoader(config_dir=tmp_path).load().config_hash
        assert changed_hash != baseline_hash

    def test_market_adapter_metadata_is_bound_into_public_config_hash(self, tmp_path: Path):
        baseline_hash = ConfigLoader(config_dir=tmp_path).load().config_hash
        _write_yaml(
            tmp_path / "system.yaml",
            {
                "market_rules": [
                    {
                        "market": "A股",
                        "tick_size": 0.01,
                        "lot_size": 100,
                        "price_precision": 2,
                        "data_sources": ["akshare", "tushare"],
                        "fee_model": "cn_stock_candidate",
                        "settlement_time": "16:00",
                    }
                ]
            },
        )
        changed_hash = ConfigLoader(config_dir=tmp_path).load().config_hash
        assert changed_hash != baseline_hash

    def test_rollover_cost_policy_is_bound_into_public_config_hash(self, tmp_path: Path):
        baseline_hash = ConfigLoader(config_dir=tmp_path).load().config_hash
        _write_yaml(
            tmp_path / "system.yaml",
            {
                "futures_rollover": {
                    "consecutive_volume_days": 3,
                    "spread_cost_bps": 9.0,
                    "commission_bps_per_leg": 0.5,
                    "require_manual_approval": True,
                }
            },
        )
        changed_hash = ConfigLoader(config_dir=tmp_path).load().config_hash
        assert changed_hash != baseline_hash


class TestGlobalLoader:
    """Test singleton and hot-reload handler."""

    def test_singleton(self):
        loader1 = get_global_loader()
        loader2 = get_global_loader()
        assert loader1 is loader2

    def test_hotreload_handler_installs(self):
        """install_hotreload_handler should not raise."""
        install_hotreload_handler()


class TestConvenienceFunction:
    """Test load_config() convenience."""

    def test_load_config_with_temp_dir(self, tmp_path: Path):
        _write_yaml(tmp_path / "system.yaml", {"env": "dev"})
        settings = load_config(tmp_path)
        assert settings.system.env == Environment.DEV
