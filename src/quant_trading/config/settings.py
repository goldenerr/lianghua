"""
Quant Trading System — Pydantic v2 Configuration Models.

All models are frozen=True to prevent runtime mutation.
All secrets use SecretStr/SecretBytes per AGENTS.md §2.
"""

from __future__ import annotations

from enum import Enum

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretBytes,
    SecretStr,
    field_validator,
    model_validator,
)

# ── Enums ────────────────────────────────────────────────────────────────────


class Environment(str, Enum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class Market(str, Enum):
    A_SHARES = "A股"
    FUTURES = "期货"
    CRYPTO = "加密货币"
    US_STOCKS = "美股"
    HK_STOCKS = "港股"
    OPTIONS = "期权"


class VaRMethod(str, Enum):
    HISTORICAL = "historical"
    MONTE_CARLO = "monte_carlo"
    PARAMETRIC = "parametric"


# ── Sub-models ───────────────────────────────────────────────────────────────


class TradingSession(BaseModel):
    """单个市场交易时段配置。UTC 内部存储。"""
    model_config = ConfigDict(frozen=True)
    market: Market
    sessions: list[tuple[str, str]]  # [(HH:MM, HH:MM), ...]
    timezone: str = "Asia/Shanghai"


class MarketRules(BaseModel):
    """单个市场的交易规则。"""
    model_config = ConfigDict(frozen=True)
    market: Market
    tick_size: float = Field(gt=0, description="最小变动价位")
    lot_size: int = Field(ge=1, description="每手数量")
    price_precision: int = Field(ge=0, le=8, description="价格精度")
    funding_rate: float | None = Field(None, description="资金费率 (crypto)")
    settlement_time: str | None = Field(None, description="结算时间 (HH:MM UTC)")


class ApiEndpointConfig(BaseModel):
    """单个交易所 API 端点配置。"""
    model_config = ConfigDict(frozen=True)
    base_url: str
    ws_url: str | None = None
    timeout_seconds: int = Field(default=10, ge=1, le=60)
    max_retries: int = Field(default=3, ge=0, le=10)
    rate_limit_rps: int = Field(default=10, ge=1, le=1000)
    circuit_breaker_failures: int = Field(default=5, ge=1)
    circuit_breaker_cooldown_seconds: int = Field(default=30, ge=5)


class ApiCredentials(BaseModel):
    """单个交易所凭证。所有敏感字段强制 SecretStr/SecretBytes。"""
    model_config = ConfigDict(frozen=True)
    exchange: str
    api_key: SecretStr
    api_secret: SecretBytes  # bytes for binary signing keys
    passphrase: SecretStr | None = None
    subaccount: str | None = None

    @field_validator("api_key")
    @classmethod
    def key_not_empty(cls, v: SecretStr) -> SecretStr:
        if not v.get_secret_value().strip():
            raise ValueError("API key must not be empty")
        return v

    @field_validator("api_secret")
    @classmethod
    def secret_not_empty(cls, v: SecretBytes) -> SecretBytes:
        if not v.get_secret_value().strip():
            raise ValueError("API secret must not be empty")
        return v


class AccountConfig(BaseModel):
    """多账户配置。"""
    model_config = ConfigDict(frozen=True)
    account_id: str
    name: str
    exchange: str
    credentials: ApiCredentials
    enabled: bool = True
    default_leverage: float = Field(default=1.0, ge=1.0, le=125.0)


# ── Top-level Settings ───────────────────────────────────────────────────────


class SystemSettings(BaseModel):
    """系统级配置 (system.yaml)"""
    model_config = ConfigDict(frozen=True)

    env: Environment = Environment.DEV
    primary_markets: list[Market] = Field(
        default=[Market.A_SHARES],
        min_length=1,
        description="主要交易市场列表"
    )
    trading_sessions: list[TradingSession] = Field(
        default_factory=list,
        description="各市场交易时段"
    )
    market_rules: list[MarketRules] = Field(
        default_factory=list,
        description="各市场交易规则"
    )
    # Graceful shutdown
    close_positions_on_shutdown: bool = False
    shutdown_timeout_seconds: int = Field(default=30, ge=5, le=300)
    # Warm-up
    warmup_bars: int = Field(default=20, ge=0, le=1000)
    # RBAC
    require_manual_approval: bool = False
    auto_trade_enabled: bool = False

    @field_validator("primary_markets")
    @classmethod
    def no_duplicate_markets(cls, v: list[Market]) -> list[Market]:
        if len(v) != len(set(v)):
            raise ValueError("primary_markets contains duplicates")
        return v


class RiskSettings(BaseModel):
    """风控参数 (risk.yaml)。每次变更必须更新此文件并提交。"""
    model_config = ConfigDict(frozen=True)

    # 仓位限制
    max_position_pct: float = Field(default=0.20, gt=0, le=1.0)
    max_total_leverage: float = Field(default=2.0, ge=1.0, le=10.0)
    # 亏损限制
    max_daily_loss_pct: float = Field(default=0.02, gt=0, le=1.0)
    max_single_loss_pct: float = Field(default=0.005, gt=0, le=1.0)
    # VaR
    var_method: VaRMethod = VaRMethod.HISTORICAL
    var_confidence: float = Field(default=0.95, ge=0.90, lt=1.0)
    max_var_pct: float = Field(default=0.05, gt=0, le=1.0)
    # 回撤
    mdd_reduce_to_50pct: float = Field(default=0.15, gt=0, le=1.0)
    mdd_liquidate_all: float = Field(default=0.25, gt=0, le=1.0)
    # 熔断
    circuit_breaker_daily_loss: float = Field(default=0.03, gt=0, le=1.0)
    circuit_breaker_daily_loss_force: float = Field(default=0.05, gt=0, le=1.0)
    vix_spike_pct: float = Field(default=0.30, gt=0)
    # 流动性
    max_volume_pct: float = Field(default=0.10, gt=0, le=1.0)
    # 均衡保护
    max_consecutive_losses: int = Field(default=5, ge=1)
    max_weekly_loss_pct: float = Field(default=0.03, gt=0, le=1.0)
    # 外汇
    max_fx_exposure_pct: float = Field(default=0.30, gt=0, le=1.0)

    @model_validator(mode="after")
    def mdd_order_consistent(self) -> RiskSettings:
        if self.mdd_reduce_to_50pct >= self.mdd_liquidate_all:
            raise ValueError("mdd_reduce_to_50pct must be < mdd_liquidate_all")
        if self.circuit_breaker_daily_loss >= self.circuit_breaker_daily_loss_force:
            raise ValueError("circuit_breaker_daily_loss must be < circuit_breaker_daily_loss_force")
        return self


class ApiSettings(BaseModel):
    """交易所 API 配置 (api.yaml)"""
    model_config = ConfigDict(frozen=True)

    exchanges: dict[str, ApiEndpointConfig] = Field(default_factory=dict)


class QuantSettings(BaseModel):
    """
    顶层配置聚合模型 — frozen=True 防止运行时修改。
    从多个 YAML 文件加载，支持环境变量覆盖。
    """
    model_config = ConfigDict(frozen=True)

    system: SystemSettings = Field(default_factory=SystemSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    accounts: list[AccountConfig] = Field(default_factory=list)
    # 运行时的系统版本清单 (core-004 填充)
    version: str = "0.1.0"
    config_hash: str = ""


# ── Validation helpers ───────────────────────────────────────────────────────


def validate_config(settings: QuantSettings) -> list[str]:
    """
    启动时校验所有必填项，类型/范围正确，且密钥不为空。
    返回错误列表，空列表表示通过。

    AGENTS.md §2: 配置校验：启动时检查所有必填项，类型/范围正确，且密钥不为空
    """
    errors: list[str] = []

    if not settings.system.primary_markets:
        errors.append("system.primary_markets must not be empty")

    try:
        RiskSettings.model_validate(settings.risk.model_dump())
    except Exception as e:
        errors.append(f"risk settings invalid: {e}")

    for i, acct in enumerate(settings.accounts):
        try:
            key_val = acct.credentials.api_key.get_secret_value()
            if not key_val.strip():
                errors.append(f"accounts[{i}].api_key is empty")
        except Exception:
            errors.append(f"accounts[{i}].api_key is missing or invalid")

    return errors
