"""
Quant Trading System — Configuration Loader.

Layered loading: base YAML → env override → accounts → Vault → env vars.
Hot-reload: SIGHUP or API endpoint reload_config().
Startup validation: validate_config() on load.

AGENTS.md §2: 所有密钥字段强制使用 SecretStr/SecretBytes
AGENTS.md §2: prod 配置必须从 Vault / AWS Secrets Manager 读取
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel

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
ModelT = TypeVar("ModelT", bound=BaseModel)
SecretResolver = Callable[[AccountConfig], ApiCredentials]
ApprovalValidator = Callable[[QuantSettings], str]

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
        secret_resolver: SecretResolver | None = None,
        approval_validator: ApprovalValidator | None = None,
    ) -> None:
        self.config_dir = Path(config_dir)
        self._settings: QuantSettings | None = None
        self._env = env
        self._secret_resolver = secret_resolver
        self._approval_validator = approval_validator

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
        system = self._load_yaml_model(SYSTEM_CONFIG, SystemSettings) or SystemSettings(
            env=target_env
        )
        risk = self._load_yaml_model(RISK_CONFIG, RiskSettings) or RiskSettings()
        api = self._load_yaml_model(API_CONFIG, ApiSettings) or ApiSettings()

        # Layer 4: Environment override
        env_path = self.config_dir / f"{target_env.value}.yaml"
        if env_path.exists():
            env_data = self._read_yaml(env_path)
            system = self._merge_override(system, env_data, SystemSettings)
        system = system.model_copy(update={"env": target_env})

        # Layer 5: Accounts
        accounts = self._load_accounts(target_env)

        # Layer 6: Vault (prod only)
        if target_env == Environment.PROD:
            accounts = self._resolve_production_secrets(accounts)

        # Layer 7: Environment variable overrides
        if target_env == Environment.PROD and (
            os.getenv("QUANT_API_KEY") or os.getenv("QUANT_API_SECRET")
        ):
            raise ValueError(
                "Production secret policy violation: environment-variable credential "
                "overrides are prohibited; use Vault, AWS Secrets Manager or sealed secrets."
            )
        if target_env != Environment.PROD:
            accounts = self._apply_env_var_secrets(accounts)

        # Build final settings
        settings = QuantSettings(
            system=system,
            risk=risk,
            api=api,
            accounts=accounts,
            config_hash=self._public_config_hash(system, risk, api, accounts),
        )

        # Startup validation
        errors = validate_config(settings)
        if errors:
            msg = "Config validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
            logger.error(msg)
            raise ValueError(msg)

        if target_env == Environment.PROD:
            settings = self._require_production_approval(settings)

        from .market_router import MarketRouter

        MarketRouter.configure(settings.system)

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

    def _load_yaml_model(self, filename: str, model_class: type[ModelT]) -> ModelT | None:
        """从 YAML 文件加载并验证为 Pydantic 模型。"""
        path = self.config_dir / filename
        if not path.exists():
            logger.warning("Config file not found: %s", path)
            return None
        data = self._read_yaml(path)
        return model_class.model_validate(data)

    def _merge_override(
        self, base: ModelT, override: dict[str, Any], model_class: type[ModelT]
    ) -> ModelT:
        """将环境覆盖合并到基础配置。"""
        base_data = base.model_dump()
        unknown = set(override) - set(base_data)
        if unknown:
            raise ValueError(f"unknown environment override keys: {sorted(unknown)}")
        base_data.update({k: v for k, v in override.items() if v is not None})
        return model_class.model_validate(base_data)

    def _load_accounts(self, target_env: Environment) -> list[AccountConfig]:
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
                declared_env = Environment(data.get("environment", Environment.DEV.value))
                if declared_env != target_env:
                    continue
                if target_env == Environment.PROD and "credentials" in data:
                    raise ValueError(
                        f"Production secret policy violation: plaintext credentials are "
                        f"prohibited in {path.name}; declare secret_ref only."
                    )
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

    def _resolve_production_secrets(self, accounts: list[AccountConfig]) -> list[AccountConfig]:
        """
        从 Vault / AWS Secrets Manager / sealed secret 解析生产密钥。
        Production files carry only a reference; injected integration code
        resolves secret material without ever persisting it here.
        """
        if self._secret_resolver is None:
            raise ValueError(
                "Production secret policy violation: a Vault/AWS/sealed secret_resolver "
                "must be configured in prod."
            )

        resolved: list[AccountConfig] = []
        for account in accounts:
            if not account.secret_ref or not account.secret_ref.startswith(
                ("vault://", "aws-sm://", "sealed://")
            ):
                raise ValueError(
                    f"Production secret policy violation: account {account.account_id} "
                    "must declare a vault://, aws-sm:// or sealed:// secret_ref."
                )
            credentials = self._secret_resolver(account)
            resolved.append(
                AccountConfig.model_validate({**account.model_dump(), "credentials": credentials})
            )
        logger.info("Production secrets resolved for %d account(s)", len(resolved))
        return resolved

    def _apply_env_var_secrets(self, accounts: list[AccountConfig]) -> list[AccountConfig]:
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
            if creds is None:
                raise ValueError(f"credentials not resolved for account: {acct.account_id}")
            new_creds = ApiCredentials(
                exchange=creds.exchange,
                api_key=SecretStr(env_key) if env_key else creds.api_key,
                api_secret=SecretBytes(env_secret.encode()) if env_secret else creds.api_secret,
                passphrase=creds.passphrase,
                subaccount=creds.subaccount,
            )
            updated.append(
                AccountConfig(
                    account_id=acct.account_id,
                    name=acct.name,
                    exchange=acct.exchange,
                    environment=acct.environment,
                    secret_ref=acct.secret_ref,
                    credentials=new_creds,
                    enabled=acct.enabled,
                    default_leverage=acct.default_leverage,
                )
            )
        return updated

    def _require_production_approval(self, settings: QuantSettings) -> QuantSettings:
        if self._approval_validator is None:
            raise ValueError(
                "Production configuration approval is required: configure an approval_validator."
            )
        approval_ref = self._approval_validator(settings).strip()
        if not approval_ref:
            raise ValueError("Production configuration approval reference must not be empty.")
        return settings.model_copy(update={"config_approval_ref": approval_ref})

    @staticmethod
    def _public_config_hash(
        system: SystemSettings,
        risk: RiskSettings,
        api: ApiSettings,
        accounts: list[AccountConfig],
    ) -> str:
        """Hash deploy-relevant configuration without credential material."""
        account_metadata = [
            {
                "account_id": account.account_id,
                "name": account.name,
                "exchange": account.exchange,
                "environment": account.environment.value,
                "secret_ref": account.secret_ref,
                "enabled": account.enabled,
                "default_leverage": account.default_leverage,
            }
            for account in accounts
        ]
        payload = {
            "system": system.model_dump(mode="json"),
            "risk": risk.model_dump(mode="json"),
            "api": api.model_dump(mode="json"),
            "accounts": account_metadata,
        }
        material = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


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
