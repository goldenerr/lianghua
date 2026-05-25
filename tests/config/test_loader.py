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
from quant_trading.config.settings import ApiCredentials, Environment

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
        assert settings.system.primary_markets == ["A股"]
        assert settings.risk.max_position_pct == 0.20
        assert settings.accounts == []

    def test_loads_partial_config(self, tmp_path: Path):
        """Should load partial config — only risk.yaml present."""
        _write_yaml(tmp_path / "risk.yaml", {
            "max_position_pct": 0.15,
            "max_total_leverage": 3.0,
        })

        loader = ConfigLoader(config_dir=tmp_path)
        settings = loader.load()

        assert settings.risk.max_position_pct == 0.15
        assert settings.risk.max_total_leverage == 3.0
        assert settings.system.primary_markets == ["A股"]  # default

    def test_loads_full_config(self, tmp_path: Path):
        """Should load system, risk, api, and account configs."""
        _write_yaml(tmp_path / "system.yaml", {
            "env": "dev",
            "primary_markets": ["A股", "加密货币"],
        })
        _write_yaml(tmp_path / "risk.yaml", {
            "max_position_pct": 0.10,
        })
        _write_yaml(tmp_path / "api.yaml", {
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
        })
        _write_yaml(tmp_path / "accounts" / "dev.yaml", {
            "account_id": "dev-001",
            "name": "Dev",
            "exchange": "binance",
            "credentials": {
                "exchange": "binance",
                "api_key": "k",
                "api_secret": "s",
            },
        })

        loader = ConfigLoader(config_dir=tmp_path)
        settings = loader.load()

        assert len(settings.system.primary_markets) == 2
        assert settings.risk.max_position_pct == 0.10
        assert "binance" in settings.api.exchanges
        assert settings.api.exchanges["binance"].timeout_seconds == 5
        assert len(settings.accounts) == 1
        assert settings.accounts[0].account_id == "dev-001"

    def test_env_override_merges(self, tmp_path: Path):
        """Environment file should override base values."""
        _write_yaml(tmp_path / "system.yaml", {
            "env": "dev",
            "auto_trade_enabled": False,
            "warmup_bars": 20,
        })
        _write_yaml(tmp_path / "dev.yaml", {
            "auto_trade_enabled": True,
        })

        loader = ConfigLoader(config_dir=tmp_path)
        settings = loader.load()

        # dev.yaml should override auto_trade_enabled
        assert settings.system.auto_trade_enabled is True
        # warmup_bars from system.yaml should be preserved
        assert settings.system.warmup_bars == 20

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

    def test_reload_clears_cache(self, tmp_path: Path):
        """reload() should invalidate cache and re-read from disk."""
        _write_yaml(tmp_path / "system.yaml", {"primary_markets": ["A股"]})

        loader = ConfigLoader(config_dir=tmp_path)
        s1 = loader.load()
        assert s1.system.primary_markets == ["A股"]

        # Modify file on disk
        _write_yaml(tmp_path / "system.yaml", {"primary_markets": ["加密货币"]})

        s2 = loader.reload()
        assert s2.system.primary_markets == ["加密货币"]


class TestEnvVarOverrides:
    """Test environment variable secret overrides."""

    def test_env_var_overrides_credentials(self, tmp_path: Path, monkeypatch):
        """QUANT_API_KEY should override all account credentials."""
        _write_yaml(tmp_path / "system.yaml", {"env": "dev"})
        _write_yaml(tmp_path / "accounts" / "dev.yaml", {
            "account_id": "dev-001",
            "name": "Dev",
            "exchange": "binance",
            "credentials": {
                "exchange": "binance",
                "api_key": "placeholder",
                "api_secret": "placeholder",
            },
        })

        monkeypatch.setenv("QUANT_API_KEY", "real_key_from_env")
        monkeypatch.setenv("QUANT_API_SECRET", "real_secret_from_env")

        loader = ConfigLoader(config_dir=tmp_path)
        settings = loader.load()

        assert settings.accounts[0].credentials.api_key.get_secret_value() == "real_key_from_env"
        assert settings.accounts[0].credentials.api_secret.get_secret_value() == b"real_secret_from_env"


class TestProdSecretPolicy:
    """Test strict prod secret policy."""

    def test_prod_without_vault_fails(self, tmp_path: Path, monkeypatch):
        _write_yaml(tmp_path / "system.yaml", {"env": "prod"})
        _write_yaml(tmp_path / "accounts" / "prod.yaml", {
            "account_id": "prod-001",
            "name": "Prod",
            "exchange": "binance",
            "credentials": {
                "exchange": "binance",
                "api_key": "k",
                "api_secret": "s",
            },
        })
        monkeypatch.delenv("VAULT_ADDR", raising=False)
        monkeypatch.delenv("QUANT_ALLOW_PLAINTEXT_PROD_SECRETS", raising=False)

        with pytest.raises(ValueError, match="VAULT_ADDR is required in prod"):
            ConfigLoader(config_dir=tmp_path).load(env=Environment.PROD)

    def test_prod_break_glass_allows_plaintext(self, tmp_path: Path, monkeypatch):
        _write_yaml(tmp_path / "system.yaml", {"env": "prod"})
        _write_yaml(tmp_path / "accounts" / "prod.yaml", {
            "account_id": "prod-001",
            "name": "Prod",
            "exchange": "binance",
            "credentials": {
                "exchange": "binance",
                "api_key": "k",
                "api_secret": "s",
            },
        })
        monkeypatch.delenv("VAULT_ADDR", raising=False)
        monkeypatch.setenv("QUANT_ALLOW_PLAINTEXT_PROD_SECRETS", "true")

        settings = ConfigLoader(config_dir=tmp_path).load(env=Environment.PROD)
        assert len(settings.accounts) == 1

    def test_prod_vault_address_without_resolver_fails(self, tmp_path: Path, monkeypatch):
        _write_yaml(tmp_path / "system.yaml", {"env": "prod"})
        _write_yaml(tmp_path / "accounts" / "prod.yaml", {
            "account_id": "prod-001",
            "name": "Prod",
            "exchange": "binance",
            "credentials": {
                "exchange": "binance",
                "api_key": "plaintext",
                "api_secret": "plaintext",
            },
        })
        monkeypatch.setenv("VAULT_ADDR", "https://vault.example")
        monkeypatch.delenv("QUANT_ALLOW_PLAINTEXT_PROD_SECRETS", raising=False)

        with pytest.raises(ValueError, match="secret_resolver"):
            ConfigLoader(config_dir=tmp_path).load(env=Environment.PROD)

    def test_prod_uses_resolved_credentials(self, tmp_path: Path, monkeypatch):
        _write_yaml(tmp_path / "system.yaml", {"env": "prod"})
        _write_yaml(tmp_path / "accounts" / "prod.yaml", {
            "account_id": "prod-001",
            "name": "Prod",
            "exchange": "binance",
            "credentials": {
                "exchange": "binance",
                "api_key": "plaintext",
                "api_secret": "plaintext",
            },
        })
        monkeypatch.setenv("VAULT_ADDR", "https://vault.example")
        monkeypatch.delenv("QUANT_ALLOW_PLAINTEXT_PROD_SECRETS", raising=False)

        def resolver(_account):
            return ApiCredentials(
                exchange="binance",
                api_key=SecretStr("vault-key"),
                api_secret=SecretBytes(b"vault-secret"),
            )

        settings = ConfigLoader(config_dir=tmp_path, secret_resolver=resolver).load(env=Environment.PROD)
        credentials = settings.accounts[0].credentials
        assert credentials.api_key.get_secret_value() == "vault-key"
        assert credentials.api_secret.get_secret_value() == b"vault-secret"


class TestValidation:
    """Test startup validation."""

    def test_empty_markets_fails(self, tmp_path: Path):
        """Empty primary_markets should raise ValueError."""
        _write_yaml(tmp_path / "system.yaml", {"primary_markets": []})

        loader = ConfigLoader(config_dir=tmp_path)
        with pytest.raises(ValueError, match="primary_markets"):
            loader.load()


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
