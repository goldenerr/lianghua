"""
Quant Trading System — Pydantic v2 Configuration Models.

All models are frozen=True to prevent runtime mutation.
All secrets use SecretStr/SecretBytes per AGENTS.md §2.
"""

from __future__ import annotations

from enum import Enum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def _clock_minute(value: str, *, allow_end_of_day: bool = False) -> int:
    """Return a validated HH:MM clock value as minutes from midnight."""
    if allow_end_of_day and value == "24:00":
        return 24 * 60
    parts = value.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError(f"invalid HH:MM value: {value}")
    hour, minute = (int(part) for part in parts)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"invalid HH:MM value: {value}")
    return hour * 60 + minute


class TradingSession(BaseModel):
    """Per-market local trading hours; runtime comparisons normalize to UTC."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    market: Market
    sessions: list[tuple[str, str]]  # [(HH:MM, HH:MM), ...]
    timezone: str = "Asia/Shanghai"

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {value}") from exc
        return value

    @model_validator(mode="after")
    def valid_sessions(self) -> TradingSession:
        if not self.sessions:
            raise ValueError("at least one trading session is required")
        for opening, closing in self.sessions:
            open_minute = _clock_minute(opening)
            close_minute = _clock_minute(closing, allow_end_of_day=True)
            if open_minute == close_minute:
                raise ValueError("trading session must not have zero duration")
        if self.market == Market.CRYPTO and self.sessions != [("00:00", "24:00")]:
            raise ValueError("crypto market must declare its 24/7 session as 00:00-24:00")
        return self


class MarketRules(BaseModel):
    """单个市场的交易规则。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    market: Market
    tick_size: float = Field(gt=0, description="最小变动价位")
    lot_size: int = Field(ge=1, description="每手数量")
    price_precision: int = Field(ge=0, le=8, description="价格精度")
    funding_rate: float | None = Field(None, description="资金费率 (crypto)")
    settlement_time: str | None = Field(None, description="市场本地结算时间 (HH:MM)")
    settlement_window_minutes: int = Field(default=30, ge=0, le=240)

    @field_validator("settlement_time")
    @classmethod
    def valid_settlement_time(cls, value: str | None) -> str | None:
        if value is not None:
            _clock_minute(value)
        return value


class ApiEndpointConfig(BaseModel):
    """单个交易所 API 端点配置。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    base_url: str
    ws_url: str | None = None
    timeout_seconds: int = Field(default=10, ge=1, le=60)
    max_retries: int = Field(default=3, ge=0, le=10)
    rate_limit_rps: int = Field(default=10, ge=1, le=1000)
    circuit_breaker_failures: int = Field(default=5, ge=1)
    circuit_breaker_cooldown_seconds: int = Field(default=30, ge=5)


class ApiCredentials(BaseModel):
    """单个交易所凭证。所有敏感字段强制 SecretStr/SecretBytes。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
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

    model_config = ConfigDict(frozen=True, extra="forbid")
    account_id: str
    name: str
    exchange: str
    environment: Environment = Environment.DEV
    secret_ref: str | None = None
    credentials: ApiCredentials | None = None
    enabled: bool = True
    default_leverage: float = Field(default=1.0, ge=1.0, le=125.0)

    @model_validator(mode="after")
    def credentials_or_reference_present(self) -> AccountConfig:
        if self.credentials is None and not (self.secret_ref and self.secret_ref.strip()):
            raise ValueError("account must provide credentials or secret_ref")
        if self.credentials is not None and self.credentials.exchange != self.exchange:
            raise ValueError("credential exchange must match account exchange")
        return self


# ── Top-level Settings ───────────────────────────────────────────────────────


class SystemSettings(BaseModel):
    """系统级配置 (system.yaml)"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    env: Environment = Environment.DEV
    primary_markets: list[Market] = Field(
        default=[Market.A_SHARES], min_length=1, description="主要交易市场列表"
    )
    trading_sessions: list[TradingSession] = Field(
        default_factory=lambda: [
            TradingSession(
                market=Market.A_SHARES,
                sessions=[("09:30", "11:30"), ("13:00", "15:00")],
                timezone="Asia/Shanghai",
            )
        ],
        description="各市场交易时段（市场本地时间）",
    )
    market_rules: list[MarketRules] = Field(
        default_factory=lambda: [
            MarketRules(
                market=Market.A_SHARES,
                tick_size=0.01,
                lot_size=100,
                price_precision=2,
                funding_rate=None,
                settlement_time="16:00",
            )
        ],
        description="各市场交易规则",
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

    @model_validator(mode="after")
    def active_markets_have_controls(self) -> SystemSettings:
        session_markets = [session.market for session in self.trading_sessions]
        rule_markets = [rule.market for rule in self.market_rules]
        if len(session_markets) != len(set(session_markets)):
            raise ValueError("trading_sessions contains duplicate markets")
        if len(rule_markets) != len(set(rule_markets)):
            raise ValueError("market_rules contains duplicate markets")
        missing_sessions = set(self.primary_markets) - set(session_markets)
        missing_rules = set(self.primary_markets) - set(rule_markets)
        if missing_sessions:
            raise ValueError(
                f"missing trading_sessions for: {sorted(market.value for market in missing_sessions)}"
            )
        if missing_rules:
            raise ValueError(
                f"missing market_rules for: {sorted(market.value for market in missing_rules)}"
            )
        return self


class RiskSettings(BaseModel):
    """风控参数 (risk.yaml)。每次变更必须更新此文件并提交。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # 仓位限制
    max_position_pct: float = Field(default=0.20, gt=0, le=1.0)
    max_per_sector: int = Field(default=5, ge=1)
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
    mdd_reduce_scale: float = Field(default=0.50, gt=0, le=1.0)
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
    strategy: StrategyRiskSettings | None = None
    gates: PerformanceGateSettings | None = None

    @model_validator(mode="after")
    def mdd_order_consistent(self) -> RiskSettings:
        if self.mdd_reduce_to_50pct >= self.mdd_liquidate_all:
            raise ValueError("mdd_reduce_to_50pct must be < mdd_liquidate_all")
        if self.circuit_breaker_daily_loss >= self.circuit_breaker_daily_loss_force:
            raise ValueError(
                "circuit_breaker_daily_loss must be < circuit_breaker_daily_loss_force"
            )
        return self


class StrategyRiskSettings(BaseModel):
    """Validated allocation parameters embedded in the risk configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    top_n: int = Field(ge=1)
    rebalance_freq_days: int = Field(ge=1)
    factors: list[str] = Field(min_length=1)
    weights: dict[str, float] = Field(min_length=1)
    sector_cap: int = Field(ge=1)

    @model_validator(mode="after")
    def factors_and_weights_consistent(self) -> StrategyRiskSettings:
        if set(self.factors) != set(self.weights):
            raise ValueError("strategy factors and weights must define identical names")
        if any(weight < 0 for weight in self.weights.values()):
            raise ValueError("strategy weights must be non-negative")
        if abs(sum(self.weights.values()) - 1.0) > 1e-6:
            raise ValueError("strategy weights must sum to 1.0")
        if self.sector_cap > self.top_n:
            raise ValueError("strategy sector_cap cannot exceed top_n")
        return self


class PerformanceGateSettings(BaseModel):
    """Validated strategy admission gates from risk.yaml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_sharpe: float = Field(ge=0)
    max_mdd: float = Field(lt=0, ge=-1.0)
    min_win_rate: float = Field(ge=0, le=1.0)
    max_decay: float = Field(ge=0, le=1.0)


RiskSettings.model_rebuild()


class ApiSettings(BaseModel):
    """交易所 API 配置 (api.yaml)"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exchanges: dict[str, ApiEndpointConfig] = Field(default_factory=dict)


class QuantSettings(BaseModel):
    """
    顶层配置聚合模型 — frozen=True 防止运行时修改。
    从多个 YAML 文件加载，支持环境变量覆盖。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    system: SystemSettings = Field(default_factory=SystemSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    accounts: list[AccountConfig] = Field(default_factory=list)
    # 运行时的系统版本清单 (core-004 填充)
    version: str = "0.1.0"
    config_hash: str = ""
    config_approval_ref: str = ""


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
    if settings.system.env == Environment.PROD and not settings.system.require_manual_approval:
        errors.append("production requires manual approval for funds-impacting operations")

    try:
        RiskSettings.model_validate(settings.risk.model_dump())
    except Exception as e:
        errors.append(f"risk settings invalid: {e}")

    for i, acct in enumerate(settings.accounts):
        if acct.credentials is None:
            errors.append(f"accounts[{i}].credentials have not been resolved")
            continue
        try:
            key_val = acct.credentials.api_key.get_secret_value()
            if not key_val.strip():
                errors.append(f"accounts[{i}].api_key is empty")
        except Exception:
            errors.append(f"accounts[{i}].api_key is missing or invalid")

    if settings.system.auto_trade_enabled and not any(
        account.enabled and account.credentials is not None for account in settings.accounts
    ):
        errors.append("automatic trading requires an enabled account with resolved credentials")

    return errors
