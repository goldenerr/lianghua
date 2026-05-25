"""Tests for data schema version management (data-004)."""
from datetime import datetime, timezone

import pytest
from quant_trading.data.schema_version import (
    CompatibilityLevel,
    CompatibilityMatrix,
    MigrationEngine,
    SchemaAuditLog,
    SchemaRegistry,
    SchemaTable,
    SchemaValidator,
    SchemaVersion,
)

UTC = timezone.utc


class TestSchemaVersion:
    def test_parse_vYYYY_MM(self):
        v = SchemaVersion.parse("v2026.05")
        assert v.year == 2026
        assert v.month == 5
        assert v.patch == 0
        assert str(v) == "v2026.05"

    def test_parse_with_patch(self):
        v = SchemaVersion.parse("v2026.05.01")
        assert v.patch == 1
        assert str(v) == "v2026.05.01"

    def test_parse_without_v_prefix(self):
        v = SchemaVersion.parse("2026.05")
        assert v.year == 2026

    def test_parse_invalid_raises(self):
        with pytest.raises(ValueError, match="Invalid schema version"):
            SchemaVersion.parse("not-a-version")

    def test_comparison(self):
        v1 = SchemaVersion.parse("v2026.01")
        v2 = SchemaVersion.parse("v2026.05")
        v3 = SchemaVersion.parse("v2026.05.01")
        assert v1 < v2 < v3
        assert v3 > v1

    def test_equality(self):
        a = SchemaVersion.parse("v2026.05")
        b = SchemaVersion.parse("v2026.05")
        assert a == b
        assert hash(a) == hash(b)

    def test_frozen(self):
        v = SchemaVersion.parse("v2026.05")
        with pytest.raises(Exception):
            v.year = 2027  # type: ignore

    def test_current(self):
        v = SchemaVersion.current()
        now = datetime.now(UTC)
        assert v.year == now.year
        assert v.month == now.month


class TestSchemaTable:
    def test_create(self):
        t = SchemaTable(
            name="test_table",
            version=SchemaVersion.parse("v2026.05"),
            columns={"col1": "Float64", "col2": "String"},
            required_columns=["col1"],
            optional_columns=["col2"],
        )
        assert t.name == "test_table"
        assert t.schema_hash is not None

    def test_schema_hash_stable(self):
        t1 = SchemaTable("t", SchemaVersion.parse("v2026.05"), {"a": "Float64"})
        t2 = SchemaTable("t", SchemaVersion.parse("v2026.05"), {"a": "Float64"})
        assert t1.schema_hash == t2.schema_hash

    def test_schema_hash_different(self):
        t1 = SchemaTable("t", SchemaVersion.parse("v2026.05"), {"a": "Float64"})
        t2 = SchemaTable("t", SchemaVersion.parse("v2026.05"), {"b": "Float64"})
        assert t1.schema_hash != t2.schema_hash


class TestCompatibilityMatrix:
    def test_same_version_full(self):
        m = CompatibilityMatrix()
        t = SchemaTable("t", SchemaVersion.parse("v2026.05"), {"a": "Float64"})
        assert m.check(t, t) == CompatibilityLevel.FULL

    def test_newer_minor_patch(self):
        m = CompatibilityMatrix()
        src = SchemaTable(
            "t", SchemaVersion.parse("v2026.05"),
            {"a": "Float64"}, required_columns=["a"],
        )
        tgt = SchemaTable(
            "t", SchemaVersion.parse("v2026.05.01"),
            {"a": "Float64", "b": "String"},
            required_columns=["a"], optional_columns=["b"],
        )
        assert m.check(src, tgt) == CompatibilityLevel.BACKWARD

    def test_added_required_column_incompatible(self):
        m = CompatibilityMatrix()
        src = SchemaTable(
            "t", SchemaVersion.parse("v2026.05"),
            {"a": "Float64"}, required_columns=["a"],
        )
        tgt = SchemaTable(
            "t", SchemaVersion.parse("v2026.06"),
            {"a": "Float64", "b": "String"},
            required_columns=["a", "b"],  # b is now required → incompatible
        )
        assert m.check(src, tgt) == CompatibilityLevel.INCOMPATIBLE

    def test_dropped_required_column_incompatible(self):
        m = CompatibilityMatrix()
        src = SchemaTable(
            "t", SchemaVersion.parse("v2026.05"),
            {"a": "Float64", "b": "Float64"},
            required_columns=["a", "b"],
        )
        tgt = SchemaTable(
            "t", SchemaVersion.parse("v2026.06"),
            {"a": "Float64", "c": "Float64"},
            required_columns=["a", "c"],
        )
        assert m.check(src, tgt) == CompatibilityLevel.INCOMPATIBLE

    def test_type_change_incompatible(self):
        m = CompatibilityMatrix()
        src = SchemaTable("t", SchemaVersion.parse("v2026.05"), {"a": "Float64"})
        tgt = SchemaTable("t", SchemaVersion.parse("v2026.06"), {"a": "String"})
        assert m.check(src, tgt) == CompatibilityLevel.INCOMPATIBLE

    def test_explicit_allowlist(self):
        m = CompatibilityMatrix()
        src = SchemaTable("t", SchemaVersion.parse("v2026.01"), {"a": "Float64"})
        tgt = SchemaTable("t", SchemaVersion.parse("v2026.12"), {"b": "String"})
        m.allow("v2026.01", "v2026.12", CompatibilityLevel.BACKWARD)
        assert m.check(src, tgt) == CompatibilityLevel.BACKWARD

    def test_optional_removed_forward_compatible(self):
        m = CompatibilityMatrix()
        src = SchemaTable(
            "t", SchemaVersion.parse("v2026.05"),
            {"a": "Float64", "b": "Float64"},
            required_columns=["a"], optional_columns=["b"],
        )
        tgt = SchemaTable(
            "t", SchemaVersion.parse("v2026.06"),
            {"a": "Float64"},
            required_columns=["a"],
        )
        assert m.check(src, tgt) == CompatibilityLevel.FORWARD


class TestSchemaRegistry:
    def test_register_and_get(self):
        reg = SchemaRegistry()
        t = SchemaTable("test", SchemaVersion.parse("v2026.05"), {"a": "Float64"})
        reg.register(t)
        assert reg.get_current("test") == t

    def test_unknown_table_returns_none(self):
        reg = SchemaRegistry()
        assert reg.get_current("nonexistent") is None

    def test_registry_defaults(self):
        reg = SchemaRegistry()
        reg.register_defaults()
        tables = reg.get_all_tables()
        assert "market_data" in tables
        assert "order_events" in tables
        assert "risk_metrics" in tables
        assert "audit_events" in tables

    def test_default_schemas_have_required_columns(self):
        reg = SchemaRegistry()
        reg.register_defaults()
        market = reg.get_current("market_data")
        assert "symbol" in market.required_columns
        assert "close" in market.required_columns


class TestSchemaValidator:
    def test_compatible_schemas_pass(self):
        reg = SchemaRegistry()
        reg.register_defaults()
        validator = SchemaValidator(reg)

        current = reg.get_current("market_data")
        data_versions = {"market_data": str(current.version)}
        reports = validator.validate(data_versions)
        assert len(reports) == 1
        assert reports[0].compatible is True

    def test_unknown_table_fails(self):
        reg = SchemaRegistry()
        validator = SchemaValidator(reg)
        reports = validator.validate({"unknown_table": "v2026.05"})
        assert reports[0].compatible is False
        assert "Unknown table" in reports[0].notes

    def test_invalid_version_fails(self):
        reg = SchemaRegistry()
        reg.register_defaults()
        validator = SchemaValidator(reg)
        reports = validator.validate({"market_data": "not-a-version"})
        assert reports[0].compatible is False

    def test_older_version_backward_compatible(self):
        reg = SchemaRegistry()
        reg.register_defaults()
        validator = SchemaValidator(reg)

        current = reg.get_current("market_data")
        older = SchemaVersion(year=current.version.year, month=current.version.month, patch=0)
        if older == current.version:
            older = SchemaVersion(year=current.version.year - 1, month=current.version.month)

        reports = validator.validate({"market_data": str(older)})
        assert reports[0].compatible is True

    def test_check_and_raise_on_failure(self):
        reg = SchemaRegistry()
        validator = SchemaValidator(reg)
        with pytest.raises(RuntimeError, match="Schema compatibility"):
            validator.check_and_raise({"unknown": "v2026.05"})

    def test_check_and_raise_passes(self):
        reg = SchemaRegistry()
        reg.register_defaults()
        validator = SchemaValidator(reg)
        current = reg.get_current("market_data")
        validator.check_and_raise({"market_data": str(current.version)})


class TestMigrationEngine:
    def test_same_version_no_migration(self):
        v = SchemaVersion.parse("v2026.05")
        t = SchemaTable("t", v, {"a": "Float64"})
        result = MigrationEngine.generate_migration(v, t)
        assert result is None

    def test_cross_month_returns_script(self):
        v = SchemaVersion.parse("v2026.01")
        t = SchemaTable("t", SchemaVersion.parse("v2026.06"), {"a": "Float64", "b": "String"})
        result = MigrationEngine.generate_migration(v, t)
        assert result is not None
        assert "Migration:" in result
        assert "WARNING" in result

    def test_apply_migration_adds_columns(self):
        stmts = MigrationEngine.apply_migration(
            "test", ["a"], {"a": "Float64", "b": "String", "c": "UInt32"}
        )
        assert len(stmts) == 2
        assert all("ADD COLUMN" in s for s in stmts)

    def test_apply_migration_no_new_columns(self):
        stmts = MigrationEngine.apply_migration("test", ["a"], {"a": "Float64"})
        assert stmts == []


class TestSchemaAuditLog:
    def test_record_and_retrieve(self):
        log = SchemaAuditLog()
        log.record("market_data", "v2026.01", "v2026.05", "add_column", {"column": "vwap"})
        history = log.get_history("market_data")
        assert len(history) == 1
        assert history[0].change_type == "add_column"

    def test_filter_by_table(self):
        log = SchemaAuditLog()
        log.record("a", "v1", "v2", "add", {})
        log.record("b", "v1", "v2", "add", {})
        assert len(log.get_history("a")) == 1
        assert len(log.get_history()) == 2

    def test_last_change(self):
        log = SchemaAuditLog()
        log.record("t", "v1", "v2", "add", {})
        log.record("t", "v2", "v3", "drop", {})
        last = log.get_last_change("t")
        assert last.to_version == "v3"

    def test_no_history(self):
        log = SchemaAuditLog()
        assert log.get_last_change("x") is None
