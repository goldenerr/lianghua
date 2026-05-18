"""
DataStore — Parquet-based storage with optional ClickHouse sync.

AGENTS.md §2 (data-001):
  历史数据 Parquet 文件（按品种/频率分区） + ClickHouse 实时数据
  数据版本控制：记录每次数据更新的时间范围和 hash
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from .provider import Frequency

logger = logging.getLogger(__name__)
UTC = timezone.utc


class DataStore:
    """
    Local Parquet data store with symbol/frequency partitioning.
    Also supports optional ClickHouse real-time sync.
    """

    def __init__(self, base_dir: str = "data/"):
        self.base_dir = Path(base_dir)
        self._version_log: list[dict] = []

    # ── Path helpers ──────────────────────────────────────────────────────

    def _path(self, symbol: str, frequency: Frequency) -> Path:
        """Get Parquet file path for a symbol/frequency."""
        safe_symbol = symbol.replace("/", "_").replace(":", "_")
        return self.base_dir / frequency.value / f"{safe_symbol}.parquet"

    def _ensure_dir(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)

    # ── Read / Write ──────────────────────────────────────────────────────

    def read(
        self,
        symbol: str,
        frequency: Frequency = Frequency.DAILY,
    ) -> pd.DataFrame:
        """Read stored data for a symbol."""
        path = self._path(symbol, frequency)
        if not path.exists():
            return pd.DataFrame()
        return pd.read_parquet(path)

    def write(
        self,
        symbol: str,
        df: pd.DataFrame,
        frequency: Frequency = Frequency.DAILY,
        *,
        append: bool = True,
    ) -> None:
        """
        Write data to Parquet store.

        If append=True, merges with existing data, deduplicates by index,
        and writes back. If append=False, overwrites.

        Returns the path written.
        """
        path = self._path(symbol, frequency)
        self._ensure_dir(path)

        df_to_write = df.copy()

        if append and path.exists():
            existing = pd.read_parquet(path)
            combined = pd.concat([existing, df_to_write])
            # Drop duplicates by index, keeping latest
            combined = combined[~combined.index.duplicated(keep="last")]
            combined = combined.sort_index()
            df_to_write = combined

        df_to_write.to_parquet(path, compression="zstd")

        # Version tracking
        if not df_to_write.empty:
            idx = df_to_write.index
            start_d = idx.min().date() if isinstance(idx, pd.DatetimeIndex) else None
            end_d = idx.max().date() if isinstance(idx, pd.DatetimeIndex) else None
            data_hash = self._hash_dataframe(df_to_write)
            self._record_version(symbol, frequency, start_d, end_d, data_hash)

    def write_batch(
        self,
        results: list[tuple[str, pd.DataFrame, Frequency]],
        *,
        append: bool = True,
    ) -> None:
        """Write multiple symbol/data pairs."""
        for symbol, df, freq in results:
            self.write(symbol, df, freq, append=append)

    # ── Version control ───────────────────────────────────────────────────

    def _hash_dataframe(self, df: pd.DataFrame) -> str:
        """Compute hash of a DataFrame for version tracking."""
        # Only hash key columns to save time
        cols = sorted([c for c in ["open", "high", "low", "close", "volume"] if c in df.columns])
        if not cols:
            cols = list(df.columns)
        data_str = df[cols].to_string()
        return hashlib.sha256(data_str.encode()).hexdigest()[:16]

    def _record_version(
        self,
        symbol: str,
        frequency: Frequency,
        start: Optional[date],
        end: Optional[date],
        data_hash: str,
    ) -> None:
        entry = {
            "symbol": symbol,
            "frequency": frequency.value,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "hash": data_hash,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        self._version_log.append(entry)

    def get_versions(self, symbol: Optional[str] = None) -> list[dict]:
        """Get version history, optionally filtered by symbol."""
        if symbol:
            return [v for v in self._version_log if v["symbol"] == symbol]
        return self._version_log

    def get_latest_version(self, symbol: str, frequency: Frequency) -> Optional[dict]:
        """Get the most recent version entry for a symbol."""
        matching = [
            v for v in self._version_log
            if v["symbol"] == symbol and v["frequency"] == frequency.value
        ]
        return matching[-1] if matching else None

    # ── Data freshness ────────────────────────────────────────────────────

    def check_freshness(
        self,
        symbol: str,
        frequency: Frequency = Frequency.DAILY,
        max_age_hours: int = 24,
    ) -> bool:
        """Check if stored data is fresh enough."""
        version = self.get_latest_version(symbol, frequency)
        if not version or not version.get("end"):
            return False

        last_end = date.fromisoformat(version["end"])
        age = date.today() - last_end
        return age.days * 24 < max_age_hours

    # ── ClickHouse sync placeholder ───────────────────────────────────────

    def sync_to_clickhouse(self, symbol: str, df: pd.DataFrame) -> None:
        """
        Placeholder: sync data to ClickHouse for real-time queries.
        AGENTS.md §2 (data-001): ClickHouse 实时数据
        """
        logger.debug("ClickHouse sync placeholder — data for %s not synced", symbol)
        # Actual implementation in monitor-001 phase:
        # from clickhouse_driver import Client
        # client = Client(host='clickhouse', database='quant')
        # client.insert_dataframe('INSERT INTO market_data VALUES', df)
