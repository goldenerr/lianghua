"""Full system regression suite (test-008)."""

from datetime import date

from quant_trading.compliance.compliance import (
    ComplianceReport,
    Jurisdiction,
    TaxCalculator,
)
from quant_trading.data.schema_version import (
    MigrationEngine,
    SchemaAuditLog,
    SchemaRegistry,
    SchemaTable,
    SchemaVersion,
)


def test_schema_migration_regression():
    registry = SchemaRegistry()
    base = SchemaTable(
        name="market_data",
        version=SchemaVersion.parse("v2026.05"),
        columns={"symbol": "String", "timestamp": "DateTime64(3)", "close": "Float64"},
        required_columns=["symbol", "timestamp", "close"],
    )
    target = SchemaTable(
        name="market_data",
        version=SchemaVersion.parse("v2026.05.01"),
        columns={
            "symbol": "String",
            "timestamp": "DateTime64(3)",
            "close": "Float64",
            "vwap": "Float64",
        },
        required_columns=["symbol", "timestamp", "close"],
        optional_columns=["vwap"],
    )
    registry.register(base)
    registry.register(target)

    statements = MigrationEngine.apply_migration(
        table_name="market_data",
        data_columns=list(base.columns.keys()),
        target_columns=target.columns,
    )
    assert statements == [
        "ALTER TABLE market_data ADD COLUMN IF NOT EXISTS vwap Float64 DEFAULT 0.0;"
    ]

    audit = SchemaAuditLog()
    audit.record(
        table_name="market_data",
        from_version=str(base.version),
        to_version=str(target.version),
        change_type="add_column",
        details={"columns": ["vwap"]},
    )
    last = audit.get_last_change("market_data")
    assert last is not None
    assert last.details["columns"] == ["vwap"]


def test_regulatory_mode_switch():
    cn_tax = TaxCalculator(Jurisdiction.CN)
    us_tax = TaxCalculator(Jurisdiction.US)

    assert cn_tax.stamp_duty(100_000) == 50.0
    assert cn_tax.capital_gains(10_000) == 0.0
    assert us_tax.stamp_duty(100_000) == 0.0
    assert us_tax.capital_gains(10_000) == 2_000.0

    cn_report = ComplianceReport(
        report_id="cn-2026-01",
        jurisdiction=Jurisdiction.CN,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        total_trades=12,
        total_volume=100_000,
        stamp_duty_paid=cn_tax.stamp_duty(100_000),
    )
    us_report = ComplianceReport(
        report_id="us-2026-01",
        jurisdiction=Jurisdiction.US,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        total_trades=12,
        total_volume=100_000,
        capital_gains_tax=us_tax.capital_gains(10_000),
    )

    assert cn_report.jurisdiction == Jurisdiction.CN
    assert us_report.jurisdiction == Jurisdiction.US
    assert b"cn-2026-01" in cn_report.to_worm()
    assert b"us-2026-01" in us_report.to_worm()
