"""
Data Schema Version Management & Compatibility Enforcement (data-004).

AGENTS.md §4 (data-004):
  - 定义数据 Schema 版本号格式，每个数据表在元数据中声明版本
  - 维护版本兼容性矩阵
  - 启动时校验 schema 兼容性
  - 不兼容则拒绝启动 + 告警 + 迁移建议
  - 提供自动迁移工具
  - 记录每次 schema 变更审计日志
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)
UTC = timezone.utc

# Version format: vYYYY.MM (e.g., v2026.05)
# Second format: vYYYY.MM.PATCH (e.g., v2026.05.01)
VERSION_RE = re.compile(r"^v(\d{4})\.(\d{2})(?:\.(\d{1,3}))?$")


# ── Version ───────────────────────────────────────────────────────────────────


@dataclass(order=True, frozen=True)
class SchemaVersion:
    """
    Immutable data schema version.

    Format: vYYYY.MM[.PATCH]
    Comparisons are chronological: v2026.05 < v2026.06 < v2026.06.01
    """

    year: int
    month: int
    patch: int = 0

    def __str__(self) -> str:
        if self.patch:
            return f"v{self.year:04d}.{self.month:02d}.{self.patch:02d}"
        return f"v{self.year:04d}.{self.month:02d}"

    @classmethod
    def parse(cls, version_str: str) -> "SchemaVersion":
        """Parse a version string like 'v2026.05' or '2026.05'."""
        s = version_str.strip()
        if not s.startswith("v"):
            s = "v" + s
        m = VERSION_RE.match(s)
        if not m:
            raise ValueError(
                f"Invalid schema version: '{version_str}'. "
                f"Expected format: vYYYY.MM[.PATCH]"
            )
        return cls(
            year=int(m.group(1)),
            month=int(m.group(2)),
            patch=int(m.group(3)) if m.group(3) else 0,
        )

    @classmethod
    def current(cls) -> "SchemaVersion":
        """Create a version from the current date."""
        now = datetime.now(UTC)
        return cls(year=now.year, month=now.month)


# ── Schema Table ──────────────────────────────────────────────────────────────


@dataclass
class SchemaTable:
    """Metadata for a single data table/schema."""

    name: str  # e.g., "market_data", "order_events"
    version: SchemaVersion
    columns: dict[str, str]  # column_name → dtype
    description: str = ""
    required_columns: list[str] = field(default_factory=list)
    optional_columns: list[str] = field(default_factory=list)

    @property
    def schema_hash(self) -> str:
        """Stable hash of the schema structure (columns + types)."""
        content = json.dumps(
            {"name": self.name, "columns": self.columns}, sort_keys=True
        )
        return hashlib.sha256(content.encode()).hexdigest()[:16]


# ── Compatibility ─────────────────────────────────────────────────────────────


class CompatibilityLevel(str, Enum):
    FULL = "full"               # Identical schema
    BACKWARD = "backward"       # New schema reads old data (added optional columns)
    FORWARD = "forward"         # Old schema reads new data (dropped optional columns)
    INCOMPATIBLE = "incompatible"  # Breaking change (renamed/dropped required columns, type change)


class CompatibilityMatrix:
    """
    Compatibility matrix: which schema versions are compatible.

    Rules:
    - Same version: FULL
    - Newer minor version (same year.month): BACKWARD if only optional columns added
    - Older patch → newer patch within same month: BACKWARD
    - Required column added/dropped/renamed: INCOMPATIBLE
    - Column type changed: INCOMPATIBLE
    - Cross-month: INCOMPATIBLE unless explicit allowlist
    """

    _allowlist: dict[tuple[str, str], CompatibilityLevel] = {}

    def allow(self, from_ver: str, to_ver: str, level: CompatibilityLevel) -> None:
        """Explicitly allow a compatibility level between two versions."""
        self._allowlist[(from_ver, to_ver)] = level

    def check(self, source: SchemaTable, target: SchemaTable) -> CompatibilityLevel:
        """
        Check compatibility from source schema → target schema.

        Args:
            source: The schema version that data was stored with.
            target: The schema version required by the running code.

        Returns:
            CompatibilityLevel indicating whether the source data can be read.
        """
        # 1. Exact match
        if source.version == target.version:
            return CompatibilityLevel.FULL

        # 2. Check allowlist
        key = (str(source.version), str(target.version))
        if key in self._allowlist:
            return self._allowlist[key]

        # 3. Check structural compatibility
        return self._structural_check(source, target)

    def _structural_check(self, source: SchemaTable, target: SchemaTable) -> CompatibilityLevel:
        """Compare column structures for compatibility."""
        src_cols = set(source.columns.keys())
        tgt_cols = set(target.columns.keys())

        # Check for renamed/dropped required columns
        src_required = set(source.required_columns)
        tgt_required = set(target.required_columns)

        # Required columns missing in target that were in source → INCOMPATIBLE
        missing_required = src_required - tgt_cols
        if missing_required:
            return CompatibilityLevel.INCOMPATIBLE

        # Required columns in target that weren't in source → INCOMPATIBLE
        new_required = tgt_required - src_cols
        if new_required:
            return CompatibilityLevel.INCOMPATIBLE

        # Type changes on overlapping columns → INCOMPATIBLE
        for col in src_cols & tgt_cols:
            if source.columns[col] != target.columns[col]:
                return CompatibilityLevel.INCOMPATIBLE

        # Only optional columns added → BACKWARD compatible
        added = tgt_cols - src_cols
        if added and all(c in target.optional_columns for c in added):
            return CompatibilityLevel.BACKWARD

        # Optional columns removed → FORWARD compatible (data has them, reader ignores)
        removed = src_cols - tgt_cols
        if removed and all(c in source.optional_columns for c in removed):
            return CompatibilityLevel.FORWARD

        # Pure additions without full optional declaration → still backward
        if added and not (src_cols - tgt_cols):
            return CompatibilityLevel.BACKWARD

        return CompatibilityLevel.INCOMPATIBLE


# ── Schema Registry ───────────────────────────────────────────────────────────


class SchemaRegistry:
    """
    Global registry of all data schemas with version tracking.

    AGENTS.md §4: 每个数据表在元数据中声明版本
    """

    def __init__(self):
        self._tables: dict[str, list[SchemaTable]] = {}  # name → versions
        self._current: dict[str, SchemaTable] = {}  # name → current version
        self.matrix = CompatibilityMatrix()

    def register(self, table: SchemaTable) -> None:
        """Register a table schema (current version)."""
        if table.name not in self._tables:
            self._tables[table.name] = []
        self._tables[table.name].append(table)
        self._current[table.name] = table

        # Same-month patches are backward compatible
        for prev in self._tables[table.name]:
            if prev.version != table.version:
                if prev.version.year == table.version.year and prev.version.month == table.version.month:
                    self.matrix.allow(
                        str(prev.version), str(table.version),
                        CompatibilityLevel.BACKWARD,
                    )

    def get_current(self, table_name: str) -> Optional[SchemaTable]:
        """Get the current schema version for a table."""
        return self._current.get(table_name)

    def get_version(self, table_name: str, version: SchemaVersion) -> Optional[SchemaTable]:
        """Get a specific historical version of a table schema."""
        for t in self._tables.get(table_name, []):
            if t.version == version:
                return t
        return None

    def get_all_tables(self) -> dict[str, SchemaTable]:
        return dict(self._current)

    def register_defaults(self) -> None:
        """Register the standard quant trading data schemas."""
        now = SchemaVersion.current()

        # market_data
        self.register(SchemaTable(
            name="market_data",
            version=now,
            columns={
                "symbol": "String", "exchange": "String",
                "timestamp": "DateTime64(3)",
                "open": "Float64", "high": "Float64",
                "low": "Float64", "close": "Float64",
                "volume": "Float64", "vwap": "Float64",
                "trades": "UInt32", "data_source": "String",
                "adjusted": "UInt8",
            },
            description="OHLCV market data (daily/minute/tick)",
            required_columns=["symbol", "timestamp", "open", "high", "low", "close", "volume"],
            optional_columns=["vwap", "trades", "data_source", "adjusted", "exchange"],
        ))

        # order_events
        self.register(SchemaTable(
            name="order_events",
            version=now,
            columns={
                "event_time": "DateTime64(3)", "client_order_id": "String",
                "exchange_order_id": "String", "strategy_id": "String",
                "symbol": "String", "order_type": "String", "side": "String",
                "quantity": "Float64", "price": "Float64",
                "status": "String", "filled_qty": "Float64",
                "avg_price": "Float64", "commission": "Float64",
                "trace_id": "String",
            },
            description="Order lifecycle events",
            required_columns=["event_time", "client_order_id", "symbol", "side", "quantity"],
            optional_columns=["exchange_order_id", "strategy_id", "order_type", "price",
                            "status", "filled_qty", "avg_price", "commission", "trace_id"],
        ))

        # risk_metrics
        self.register(SchemaTable(
            name="risk_metrics",
            version=now,
            columns={
                "timestamp": "DateTime64(3)", "strategy_id": "String",
                "var_95": "Float64", "sharpe_20d": "Float64",
                "max_drawdown": "Float64", "total_exposure": "Float64",
                "leverage": "Float64", "daily_pnl": "Float64",
                "total_equity": "Float64",
            },
            description="Risk metrics timeseries",
            required_columns=["timestamp", "strategy_id", "var_95", "total_exposure", "total_equity"],
            optional_columns=["sharpe_20d", "max_drawdown", "leverage", "daily_pnl"],
        ))

        # audit_events
        self.register(SchemaTable(
            name="audit_events",
            version=now,
            columns={
                "event_time": "DateTime64(3)", "event_type": "String",
                "source": "String", "trace_id": "String",
                "payload": "String", "signature": "String",
            },
            description="Immutable audit trail",
            required_columns=["event_time", "event_type", "source", "trace_id"],
            optional_columns=["payload", "signature"],
        ))

        logger.info(
            "Default schemas registered: %s — version %s",
            list(self._current.keys()), now,
        )


# ── Schema Validator ─────────────────────────────────────────────────────────


@dataclass
class CompatibilityReport:
    """Result of a schema compatibility check."""

    table_name: str
    data_version: SchemaVersion
    required_version: SchemaVersion
    level: CompatibilityLevel
    compatible: bool
    migration_available: bool = False
    migration_script: str = ""
    notes: str = ""


class SchemaValidator:
    """
    Startup schema compatibility validator.

    AGENTS.md §4 (data-004):
      回测/实盘启动时，读取策略配置中声明的所需 schema 版本，
      与当前数据 schema 版本比对。兼容则运行，不兼容则拒绝启动+告警+迁移建议。
    """

    def __init__(self, registry: SchemaRegistry):
        self.registry = registry

    def validate(
        self,
        data_versions: dict[str, str],  # table_name → schema version string
        required_version: Optional[str] = None,
    ) -> list[CompatibilityReport]:
        """
        Validate all data schemas against current registry.

        Args:
            data_versions: Dict of table_name → version_string for stored data.
            required_version: If set, check against this version instead of current.

        Returns:
            List of CompatibilityReport — any with compatible=False blocks startup.
        """
        reports: list[CompatibilityReport] = []

        for table_name, data_ver_str in data_versions.items():
            try:
                data_ver = SchemaVersion.parse(data_ver_str)
            except ValueError as e:
                reports.append(CompatibilityReport(
                    table_name=table_name,
                    data_version=SchemaVersion(0, 0),
                    required_version=SchemaVersion(0, 0),
                    level=CompatibilityLevel.INCOMPATIBLE,
                    compatible=False,
                    notes=f"Invalid version format: {e}",
                ))
                continue

            current = self.registry.get_current(table_name)
            if current is None:
                reports.append(CompatibilityReport(
                    table_name=table_name,
                    data_version=data_ver,
                    required_version=SchemaVersion(0, 0),
                    level=CompatibilityLevel.INCOMPATIBLE,
                    compatible=False,
                    notes=f"Unknown table: '{table_name}' not in registry",
                ))
                continue

            # Build a synthetic source table for the stored data version
            stored = self.registry.get_version(table_name, data_ver)
            if stored is None:
                # Historical version not found — try fuzzy: check if it's in the version list
                known = [str(t.version) for t in self.registry._tables.get(table_name, [])]
                # If version is older than all known, assume backward compatible
                if data_ver < current.version:
                    level = CompatibilityLevel.BACKWARD
                    compatible = True
                    notes = f"Data version {data_ver} predates registry. Assuming backward compatible."
                else:
                    level = CompatibilityLevel.INCOMPATIBLE
                    compatible = False
                    notes = f"Version {data_ver} not found in registry (known: {known})."
            else:
                level = self.registry.matrix.check(stored, current)
                compatible = level != CompatibilityLevel.INCOMPATIBLE
                notes = f"Compatibility: {level.value}"

            report = CompatibilityReport(
                table_name=table_name,
                data_version=data_ver,
                required_version=current.version,
                level=level,
                compatible=compatible,
                notes=notes,
            )

            # Check for migration
            if not compatible:
                migration = MigrationEngine.generate_migration(data_ver, current)
                report.migration_available = migration is not None
                report.migration_script = migration or ""

            reports.append(report)

        return reports

    def check_and_raise(self, data_versions: dict[str, str]) -> None:
        """Validate and raise RuntimeError if any table is incompatible."""
        reports = self.validate(data_versions)
        failures = [r for r in reports if not r.compatible]

        if failures:
            msg_parts = ["Schema compatibility check FAILED:"]
            for r in failures:
                msg_parts.append(
                    f"  {r.table_name}: data={r.data_version} required={r.required_version} "
                    f"— {r.notes}"
                )
            if any(r.migration_available for r in failures):
                msg_parts.append("\nMigration scripts available — run migration tool first.")
            raise RuntimeError("\n".join(msg_parts))

        logger.info(
            "Schema compatibility check PASSED: %d tables verified",
            len(reports),
        )


# ── Migration Engine ──────────────────────────────────────────────────────────


class MigrationEngine:
    """
    Generate and apply data migration scripts.

    AGENTS.md §4 (data-004):
      提供自动迁移工具：对于可自动迁移的变更（如字段新增），可生成迁移脚本并执行
    """

    @staticmethod
    def generate_migration(
        from_version: SchemaVersion,
        to_table: SchemaTable,
    ) -> Optional[str]:
        """
        Generate a migration script for compatible changes.

        Currently supports:
        - Adding optional columns (backward compatible, no migration needed)
        - Column renames (manual migration required)

        Returns:
            Migration script string, or None if migration not possible.
        """
        # If versions are the same, no migration needed
        if from_version == to_table.version:
            return None

        # For same-month patches, they're backward compatible — no migration needed
        if from_version.year == to_table.version.year and from_version.month == to_table.version.month:
            return None

        # For cross-month: generate migration notes but can't auto-migrate
        return (
            f"# Migration: {to_table.name} {from_version} → {to_table.version}\n"
            f"# WARNING: Cross-version migration may require manual intervention.\n"
            f"#\n"
            f"# 1. Backup data before migration.\n"
            f"# 2. Review column changes: {list(to_table.columns.keys())}\n"
            f"# 3. Run: quant-cli migrate {to_table.name} --from {from_version} --to {to_table.version}\n"
            f"# 4. Verify with: quant-cli validate-schema\n"
        )

    @staticmethod
    def apply_migration(
        table_name: str,
        data_columns: list[str],
        target_columns: dict[str, str],
    ) -> list[str]:
        """
        Determine which columns to add/default for a migration.
        Returns list of SQL ALTER TABLE ADD COLUMN statements.
        """
        existing = set(data_columns)
        target = set(target_columns.keys())
        to_add = target - existing

        statements = []
        for col in sorted(to_add):
            dtype = target_columns[col]
            default = "NULL"
            if dtype.startswith("Float"):
                default = "0.0"
            elif dtype.startswith("Int") or dtype.startswith("UInt"):
                default = "0"
            elif dtype == "String":
                default = "''"
            statements.append(
                f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS "
                f"{col} {dtype} DEFAULT {default};"
            )

        return statements


# ── Schema Audit Log ──────────────────────────────────────────────────────────


@dataclass
class SchemaChangeEntry:
    """A single schema change audit entry."""

    timestamp: datetime
    table_name: str
    from_version: str
    to_version: str
    change_type: str  # "add_column", "drop_column", "rename_column", "type_change"
    details: dict
    operator: str = "system"


class SchemaAuditLog:
    """
    Track all schema changes for audit purposes.

    AGENTS.md §4 (data-004): 记录每次 schema 变更的审计日志
    """

    def __init__(self):
        self._entries: list[SchemaChangeEntry] = []

    def record(
        self,
        table_name: str,
        from_version: str,
        to_version: str,
        change_type: str,
        details: dict,
    ) -> None:
        """Record a schema change."""
        entry = SchemaChangeEntry(
            timestamp=datetime.now(UTC),
            table_name=table_name,
            from_version=from_version,
            to_version=to_version,
            change_type=change_type,
            details=details,
        )
        self._entries.append(entry)
        logger.info(
            "Schema change: %s %s → %s (%s): %s",
            table_name, from_version, to_version, change_type, details,
        )

    def get_history(self, table_name: Optional[str] = None) -> list[SchemaChangeEntry]:
        """Get change history, optionally filtered by table."""
        if table_name:
            return [e for e in self._entries if e.table_name == table_name]
        return list(self._entries)

    def get_last_change(self, table_name: str) -> Optional[SchemaChangeEntry]:
        """Get the most recent change for a table."""
        for e in reversed(self._entries):
            if e.table_name == table_name:
                return e
        return None
