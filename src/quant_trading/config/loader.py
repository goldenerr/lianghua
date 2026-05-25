"""
Quant Trading System — Configuration Loader.

Layered loading: base YAML → env override → accounts → Vault → env vars.
Hot-reload: SIGHUP or API endpoint reload_config().
Startup validation: validate_config() on load.

AGENTS.md §2: 所有密钥字段强制使用 SecretStr/SecretBytes
AGENTS.md §2: prod 配置必须从 Vault / AWS Secrets Manager 读取
"""

from __future__ import annotations

import logging
import os
import signal
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from .settings import (
    AccountConfig,
    ApiCredentials,
    ApiSettings,
    Environment,
    QuantSettings,
    RiskSettings,
    SystemSettings,
    validate_config,
)

logger = logging.getLogger(__name__)

# Default config paths
DEFAULT_CONFIG_DIR = Path("config")
SYSTEM_CONFIG = "system.yaml"
RISK_CONFIG = "risk.yaml"
API_CONFIG = "api.yaml"
ACCOUNTS_DIR = "accounts"


class ConfigLoader:
    """
    配置加载器。支持分层加载和热重载。

    Layers (优先级从低到高):
    1. <config_dir>/system.yaml  — 基础系统配置
    2. <config_dir>/risk.yaml     — 风控参数
    3. <config_dir>/api.yaml      — API 端点
    4. <config_dir>/<env>.yaml    — 环境覆盖
    5. <config_dir>/accounts/*.yaml — 账户配置
    6. Vault / AWS Secrets Manager — 生产密钥 (prod only)
    7. 环境变量 QUANT_*           — 最高优先级覆盖
    """

    def __init__(
        self,
        config_dir: Path | str = DEFAULT_CONFIG_DIR,
        env: Environment | None = None,
        secret_resolver: Callable[[AccountConfig], ApiCredentials] | None = None,
    ):
        self.config_dir = Path(config_dir)
        self._settings: QuantSettings | None = None
        self._env = env
        self._secret_resolver = secret_resolver

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def settings(self) -> QuantSettings:
        """获取当前配置。首次调用时加载。"""
        if self._settings is None:
            self._settings = self.load()
        return self._settings

    def load(self, env: Environment | None = None) -> QuantSettings:
        """加载并验证完整配置。"""
        target_env = env or self._env or self._detect_env()
        self._env = target_env

        # Layer 1-3: Base configs
        system = self._load_yaml_model(
            SYSTEM_CONFIG, SystemSettings
        ) or SystemSettings(env=target_env)
        risk = self._load_yaml_model(RISK_CONFIG, RiskSettings) or RiskSettings()
        api = self._load_yaml_model(API_CONFIG, ApiSettings) or ApiSettings()

        # Layer 4: Environment override
        env_path = self.config_dir / f"{target_env.value}.yaml"
        if env_path.exists():
            env_data = self._read_yaml(env_path)
            system = self._merge_override(system, env_data, SystemSettings)

        # Layer 5: Accounts
        accounts = self._load_accounts()

        # Layer 6: Vault (prod only)
        if target_env == Environment.PROD:
            accounts = self._resolve_secrets_from_vault(accounts)

        # Layer 7: Environment variable overrides
        accounts = self._apply_env_var_secrets(accounts)

        # Build final settings
        settings = QuantSettings(
            system=system,
            risk=risk,
            api=api,
            accounts=accounts,
        )

        # Startup validation
        errors = validate_config(settings)
        if errors:
            msg = "Config validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
            logger.error(msg)
            raise ValueError(msg)

        logger.info(
            "Configuration loaded: env=%s markets=%s accounts=%d",
            target_env.value,
            [m.value for m in system.primary_markets],
            len(accounts),
        )
        return settings

    def reload(self) -> QuantSettings:
        """热重载配置 (通过 SIGHUP 或 API 触发)。"""
        logger.info("Hot-reloading configuration...")
        self._settings = None
        return self.load()

    # ── Internal ──────────────────────────────────────────────────────────

    def _detect_env(self) -> Environment:
        """从环境变量 QUANT_ENV 或 config_dir 下的 yaml 推断环境。"""
        env_var = os.getenv("QUANT_ENV", "").lower()
        try:
            return Environment(env_var)
        except ValueError:
            pass
        # Fallback: check what env files exist
        for e in Environment:
            if (self.config_dir / f"{e.value}.yaml").exists():
                return e
        return Environment.DEV

    def _read_yaml(self, path: Path) -> dict[str, Any]:
        """读取 YAML 文件，返回字典。"""
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            logger.debug("Config file not found: %s", path)
            return {}
        except yaml.YAMLError as e:
            logger.error("YAML parse error in %s: %s", path, e)
            raise

    def _load_yaml_model(self, filename: str, model_class: type) -> Any | None:
        """从 YAML 文件加载并验证为 Pydantic 模型。"""
        path = self.config_dir / filename
        if not path.exists():
            logger.warning("Config file not found: %s", path)
            return None
        data = self._read_yaml(path)
        return model_class.model_validate(data)

    def _merge_override(self, base: Any, override: dict, model_class: type) -> Any:
        """将环境覆盖合并到基础配置。"""
        base_data = base.model_dump()
        base_data.update({k: v for k, v in override.items() if k in base_data and v is not None})
        return model_class.model_validate(base_data)

    def _load_accounts(self) -> list[AccountConfig]:
        """从 config/accounts/ 加载所有账户配置。"""
        accounts_dir = self.config_dir / ACCOUNTS_DIR
        if not accounts_dir.exists():
            return []

        accounts: list[AccountConfig] = []
        for path in sorted(accounts_dir.glob("*.yaml")):
            if path.name.endswith(".example.yaml"):
                continue  # 跳过示例文件
            try:
                data = self._read_yaml(path)
                # credentials 子对象
                if "credentials" in data:
                    data["credentials"] = ApiCredentials.model_validate(data["credentials"])
                acct = AccountConfig.model_validate(data)
                accounts.append(acct)
                logger.debug("Loaded account: %s from %s", acct.account_id, path.name)
            except Exception as e:
                logger.error("Failed to load account %s: %s", path.name, e)
                raise
        return accounts

    def _resolve_secrets_from_vault(
        self, accounts: list[AccountConfig]
    ) -> list[AccountConfig]:
        """
        从 Vault / AWS Secrets Manager 解析生产密钥。
        AGENTS.md §2: prod 配置必须从 Vault 读取。
        The actual Vault or Secrets Manager client is supplied through
        ``secret_resolver`` so this loader never silently authenticates with
        credentials loaded from production files.
        """
        vault_addr = os.getenv("VAULT_ADDR")
        break_glass = os.getenv("QUANT_ALLOW_PLAINTEXT_PROD_SECRETS", "").lower() in {"1", "true", "yes"}
        if break_glass:
            logger.warning(
                "QUANT_ALLOW_PLAINTEXT_PROD_SECRETS is enabled. "
                "Using file credentials in prod (emergency-only path)."
            )
            return accounts

        if not vault_addr:
            raise ValueError(
                "Production secret policy violation: VAULT_ADDR is required in prod. "
                "Set QUANT_ALLOW_PLAINTEXT_PROD_SECRETS=true only for emergency break-glass use."
            )

        if self._secret_resolver is None:
            raise ValueError(
                "Production secret policy violation: a Vault/AWS secret_resolver "
                "must be configured when VAULT_ADDR is set."
            )

        resolved: list[AccountConfig] = []
        for account in accounts:
            credentials = self._secret_resolver(account)
            resolved.append(account.model_copy(update={"credentials": credentials}))
        logger.info("Production secrets resolved for %d account(s)", len(resolved))
        return resolved

    def _apply_env_var_secrets(
        self, accounts: list[AccountConfig]
    ) -> list[AccountConfig]:
        """
        环境变量覆盖密钥 (最高优先级)。
        QUANT_API_KEY / QUANT_API_SECRET 可覆盖所有账户的密钥。
        """
        env_key = os.getenv("QUANT_API_KEY")
        env_secret = os.getenv("QUANT_API_SECRET")

        if not env_key and not env_secret:
            return accounts

        from pydantic import SecretBytes, SecretStr

        updated: list[AccountConfig] = []
        for acct in accounts:
            creds = acct.credentials
            new_creds = ApiCredentials(
                exchange=creds.exchange,
                api_key=SecretStr(env_key) if env_key else creds.api_key,
                api_secret=SecretBytes(env_secret.encode()) if env_secret else creds.api_secret,
                passphrase=creds.passphrase,
                subaccount=creds.subaccount,
            )
            updated.append(AccountConfig(
                account_id=acct.account_id,
                name=acct.name,
                exchange=acct.exchange,
                credentials=new_creds,
                enabled=acct.enabled,
                default_leverage=acct.default_leverage,
            ))
        return updated


# ── Hot-reload via SIGHUP ────────────────────────────────────────────────────

_global_loader: ConfigLoader | None = None


def get_global_loader() -> ConfigLoader:
    """获取全局 ConfigLoader 单例。"""
    global _global_loader
    if _global_loader is None:
        _global_loader = ConfigLoader()
    return _global_loader


def _on_sighup(signum: int, frame: Any) -> None:
    """SIGHUP 信号处理：触发热重载。"""
    logger.info("Received SIGHUP — reloading configuration")
    try:
        get_global_loader().reload()
    except Exception as e:
        logger.error("Hot-reload failed: %s", e, exc_info=True)


def install_hotreload_handler() -> None:
    """注册 SIGHUP 信号处理器用于配置热重载。"""
    signal.signal(signal.SIGHUP, _on_sighup)
    logger.info("Hot-reload handler installed (SIGHUP)")


# ── Convenience ──────────────────────────────────────────────────────────────

def load_config(config_dir: Path | str = DEFAULT_CONFIG_DIR) -> QuantSettings:
    """快速加载配置。"""
    return ConfigLoader(config_dir).load()
