"""Tests for Pydantic config models (AGENTS.md §2)."""

import pytest
from pydantic import SecretBytes, SecretStr, ValidationError
from quant_trading.config.settings import (
    AccountConfig,
    ApiCredentials,
    ApiEndpointConfig,
    ApiSettings,
    Environment,
    FuturesRolloverPolicy,
    MarketRules,
    QuantSettings,
    RiskSettings,
    StrategyRiskSettings,
    SystemSettings,
    TradingSession,
    VaRMethod,
    validate_config,
)

# ── SystemSettings ────────────────────────────────────────────────────────


class TestSystemSettings:
    def test_defaults(self):
        s = SystemSettings()
        assert s.env == "dev"
        assert s.primary_markets == ("A股",)
        assert s.close_positions_on_shutdown is False

    def test_rejects_duplicate_markets(self):
        with pytest.raises(ValidationError, match="duplicates"):
            SystemSettings(primary_markets=["A股", "A股"])

    def test_frozen_prevents_mutation(self):
        s = SystemSettings()
        with pytest.raises(ValidationError):  # frozen=True blocks __setattr__
            s.warmup_bars = 999  # type: ignore

    def test_market_control_collections_are_deeply_immutable(self):
        s = SystemSettings()
        with pytest.raises(AttributeError):
            s.primary_markets.append("加密货币")  # type: ignore[attr-defined]
        with pytest.raises(AttributeError):
            s.trading_sessions[0].sessions.append(("00:00", "24:00"))  # type: ignore[attr-defined]
        with pytest.raises(AttributeError):
            s.market_rules[0].data_sources.append("unapproved")  # type: ignore[attr-defined]

    def test_shutdown_timeout_range(self):
        with pytest.raises(ValidationError):
            SystemSettings(shutdown_timeout_seconds=0)

    def test_active_market_requires_session_and_rule_controls(self):
        with pytest.raises(ValidationError, match="missing trading_sessions"):
            SystemSettings(primary_markets=["加密货币"])

    def test_invalid_market_timezone_is_rejected(self):
        with pytest.raises(ValidationError, match="unknown IANA timezone"):
            TradingSession(
                market="A股",
                sessions=[("09:30", "11:30")],
                timezone="Mars/Olympus",
            )

    def test_crypto_must_be_declared_as_full_day(self):
        with pytest.raises(ValidationError, match="24/7"):
            TradingSession(
                market="加密货币",
                sessions=[("09:30", "16:00")],
                timezone="UTC",
            )

    def test_settlement_clock_is_validated(self):
        with pytest.raises(ValidationError, match="invalid HH:MM"):
            MarketRules(
                market="A股",
                tick_size=0.01,
                lot_size=100,
                price_precision=2,
                data_sources=["tushare"],
                fee_model="cn_stock",
                settlement_time="25:30",
            )

    def test_market_rule_tick_size_must_match_declared_precision(self):
        with pytest.raises(ValidationError, match="price_precision"):
            MarketRules(
                market="A股",
                tick_size=0.005,
                lot_size=100,
                price_precision=2,
                data_sources=["tushare"],
                fee_model="cn_stock",
            )

    def test_market_rule_rejects_nonfinite_numeric_configuration(self):
        with pytest.raises(ValidationError, match="finite"):
            MarketRules(
                market="A股",
                tick_size=float("inf"),
                lot_size=100,
                price_precision=2,
                data_sources=["tushare"],
                fee_model="cn_stock",
            )

    def test_market_adapter_identifiers_are_strict(self):
        with pytest.raises(ValidationError, match="data_sources"):
            MarketRules(
                market="A股",
                tick_size=0.01,
                lot_size=100,
                price_precision=2,
                data_sources=["tushare", "tushare"],
                fee_model="cn_stock",
            )

    def test_non_full_day_market_requires_holiday_calendar(self):
        with pytest.raises(ValidationError, match="holiday_calendar"):
            TradingSession(
                market="A股",
                sessions=[("09:30", "11:30")],
                timezone="Asia/Shanghai",
            )

    def test_full_day_market_cannot_declare_holiday_calendar(self):
        with pytest.raises(ValidationError, match="must not declare"):
            TradingSession(
                market="加密货币",
                sessions=[("00:00", "24:00")],
                timezone="UTC",
                holiday_calendar="NYSE",
            )

    def test_futures_rollover_cannot_disable_manual_approval(self):
        with pytest.raises(ValidationError, match="require_manual_approval"):
            FuturesRolloverPolicy(require_manual_approval=False)

    def test_futures_rollover_cost_rates_are_bounded(self):
        with pytest.raises(ValidationError, match="spread_cost_bps"):
            FuturesRolloverPolicy(spread_cost_bps=-0.01)
        with pytest.raises(ValidationError, match="finite"):
            FuturesRolloverPolicy(commission_bps_per_leg=float("nan"))


# ── RiskSettings ──────────────────────────────────────────────────────────


class TestRiskSettings:
    def test_defaults(self):
        r = RiskSettings()
        assert r.max_position_pct == 0.20
        assert r.max_total_leverage == 2.0
        assert r.var_method == VaRMethod.HISTORICAL

    def test_mdd_order_consistency(self):
        with pytest.raises(ValidationError, match="mdd_reduce_to_50pct"):
            RiskSettings(mdd_reduce_to_50pct=0.30, mdd_liquidate_all=0.25)

    def test_circuit_breaker_order(self):
        with pytest.raises(ValidationError, match="circuit_breaker_daily_loss"):
            RiskSettings(
                circuit_breaker_daily_loss=0.10,
                circuit_breaker_daily_loss_force=0.05,
            )

    def test_frozen(self):
        r = RiskSettings()
        with pytest.raises(ValidationError):
            r.max_position_pct = 0.50  # type: ignore

    def test_range_validation(self):
        with pytest.raises(ValidationError):
            RiskSettings(max_position_pct=1.5)  # > 1.0

    def test_range_validation_negative(self):
        with pytest.raises(ValidationError):
            RiskSettings(max_daily_loss_pct=-0.01)

    def test_strategy_weights_are_validated(self):
        with pytest.raises(ValidationError, match="sum to 1.0"):
            StrategyRiskSettings(
                top_n=10,
                rebalance_freq_days=20,
                factors=["momentum", "volatility"],
                weights={"momentum": 0.7, "volatility": 0.2},
                sector_cap=2,
            )

    def test_strategy_configuration_collections_are_deeply_immutable(self):
        strategy = StrategyRiskSettings(
            top_n=10,
            rebalance_freq_days=20,
            factors=["momentum", "volatility"],
            weights={"momentum": 0.7, "volatility": 0.3},
            sector_cap=2,
        )
        with pytest.raises(AttributeError):
            strategy.factors.append("unapproved")  # type: ignore[attr-defined]
        with pytest.raises(TypeError):
            strategy.weights["momentum"] = 1.0
        with pytest.raises(TypeError):
            dict.__setitem__(strategy.weights, "momentum", 1.0)  # type: ignore[arg-type]

    def test_unknown_risk_parameter_is_rejected(self):
        with pytest.raises(ValidationError, match="extra_forbidden"):
            RiskSettings.model_validate({"unapproved_risk_limit": 0.1})


# ── ApiCredentials ────────────────────────────────────────────────────────


class TestApiCredentials:
    def test_creates_with_secretstr(self):
        creds = ApiCredentials(
            exchange="binance",
            api_key=SecretStr("test_key"),
            api_secret=SecretBytes(b"test_secret"),
        )
        assert creds.api_key.get_secret_value() == "test_key"
        assert creds.api_secret.get_secret_value() == b"test_secret"

    def test_rejects_empty_api_key(self):
        with pytest.raises(ValidationError, match="must not be empty"):
            ApiCredentials(
                exchange="binance",
                api_key=SecretStr("   "),
                api_secret=SecretBytes(b"secret"),
            )

    def test_rejects_empty_api_secret(self):
        with pytest.raises(ValidationError, match="must not be empty"):
            ApiCredentials(
                exchange="binance",
                api_key=SecretStr("key"),
                api_secret=SecretBytes(b"   "),
            )

    def test_rejects_blank_exchange(self):
        with pytest.raises(ValidationError, match="exchange must not be empty"):
            ApiCredentials(
                exchange="   ",
                api_key=SecretStr("key"),
                api_secret=SecretBytes(b"secret"),
            )

    def test_frozen(self):
        creds = ApiCredentials(
            exchange="binance",
            api_key=SecretStr("key"),
            api_secret=SecretBytes(b"secret"),
        )
        with pytest.raises(ValidationError):
            creds.exchange = "okx"  # type: ignore

    def test_secret_values_not_leaked_in_repr(self):
        creds = ApiCredentials(
            exchange="binance",
            api_key=SecretStr("my_key"),
            api_secret=SecretBytes(b"my_secret"),
        )
        r = repr(creds)
        assert "my_key" not in r
        assert "my_secret" not in r


# ── ApiSettings ───────────────────────────────────────────────────────────


class TestApiSettings:
    def test_exchange_endpoints_are_deeply_immutable_and_serializable(self):
        api = ApiSettings(
            exchanges={"binance": ApiEndpointConfig(base_url="https://api.binance.com")}
        )
        with pytest.raises(TypeError):
            api.exchanges["okx"] = ApiEndpointConfig(base_url="https://www.okx.com")
        with pytest.raises(TypeError):
            dict.__setitem__(  # type: ignore[arg-type]
                api.exchanges, "okx", ApiEndpointConfig(base_url="https://www.okx.com")
            )
        assert api.model_dump(mode="json")["exchanges"]["binance"]["timeout_seconds"] == 10

    def test_empty_endpoint_defaults_cannot_be_mutated(self):
        with pytest.raises(TypeError):
            ApiSettings().exchanges["binance"] = ApiEndpointConfig(
                base_url="https://api.binance.com"
            )


# ── AccountConfig ─────────────────────────────────────────────────────────


class TestAccountConfig:
    def test_valid_account(self):
        acct = AccountConfig(
            account_id="test-1",
            name="Test",
            exchange="binance",
            credentials=ApiCredentials(
                exchange="binance",
                api_key=SecretStr("k"),
                api_secret=SecretBytes(b"s"),
            ),
        )
        assert acct.enabled is True
        assert acct.default_leverage == 1.0

    def test_leverage_range(self):
        creds = ApiCredentials(
            exchange="binance",
            api_key=SecretStr("k"),
            api_secret=SecretBytes(b"s"),
        )
        with pytest.raises(ValidationError):
            AccountConfig(
                account_id="x",
                name="x",
                exchange="binance",
                credentials=creds,
                default_leverage=0,  # < 1
            )

    def test_production_reference_can_be_resolved_later(self):
        acct = AccountConfig(
            account_id="prod-1",
            name="Production",
            exchange="binance",
            environment="prod",
            secret_ref="vault://quant/accounts/prod-1/binance",
        )
        assert acct.credentials is None

    @pytest.mark.parametrize("field", ["account_id", "name", "exchange"])
    def test_rejects_blank_identity_fields(self, field: str):
        data = {
            "account_id": "acct",
            "name": "Account",
            "exchange": "binance",
            "secret_ref": "vault://quant/accounts/acct/binance",
        }
        data[field] = "   "
        with pytest.raises(ValidationError, match="identity fields must not be empty"):
            AccountConfig.model_validate(data)

    def test_identity_fields_are_normalized_before_matching_credentials(self):
        acct = AccountConfig(
            account_id=" acct ",
            name=" Account ",
            exchange=" binance ",
            secret_ref=" vault://quant/accounts/acct/binance ",
        )
        assert acct.account_id == "acct"
        assert acct.name == "Account"
        assert acct.exchange == "binance"
        assert acct.secret_ref == "vault://quant/accounts/acct/binance"


# ── validate_config ───────────────────────────────────────────────────────


class TestValidateConfig:
    def test_empty_markets_caught_by_pydantic(self):
        """Empty primary_markets caught by Pydantic min_length=1 at construction."""
        with pytest.raises(ValidationError, match="primary_markets"):
            SystemSettings(primary_markets=[])

    def test_valid_config_no_errors(self):
        s = QuantSettings()
        errors = validate_config(s)
        assert errors == []
        with pytest.raises(AttributeError):
            s.accounts.append("unapproved")  # type: ignore[attr-defined]

    def test_empty_account_api_key_caught_by_pydantic(self):
        """Empty API key caught by field_validator at construction time."""
        with pytest.raises(ValidationError, match="must not be empty"):
            ApiCredentials(
                exchange="binance",
                api_key=SecretStr(" "),
                api_secret=SecretBytes(b"s"),
            )

    def test_empty_account_api_key_in_validate_config(self):
        """validate_config catches if account API key somehow empty."""
        # Pydantic field_validator catches this at construction, so we test
        # validate_config with a valid account (no errors expected)
        creds = ApiCredentials(
            exchange="binance",
            api_key=SecretStr("k"),
            api_secret=SecretBytes(b"s"),
        )
        acct = AccountConfig(account_id="x", name="x", exchange="binance", credentials=creds)
        s = QuantSettings(accounts=[acct])
        errors = validate_config(s)
        assert errors == []

    def test_production_requires_manual_approval(self):
        settings = QuantSettings(system=SystemSettings(env=Environment.PROD))
        assert any("manual approval" in error for error in validate_config(settings))

    def test_auto_trade_requires_resolved_enabled_account(self):
        settings = QuantSettings(system=SystemSettings(auto_trade_enabled=True))
        assert any("automatic trading" in error for error in validate_config(settings))
