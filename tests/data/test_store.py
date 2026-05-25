"""Tests for DataStore — Parquet persistence with version control."""

from pathlib import Path

import pandas as pd
from quant_trading.data.provider import Frequency
from quant_trading.data.store import DataStore


def _make_df(dates=None, values=None):
    if dates is None:
        dates = pd.bdate_range("2024-01-01", periods=5, freq="B")
    if values is None:
        values = list(range(100, 105))
    return pd.DataFrame(
        {"open": values, "high": [v + 1 for v in values],
         "low": [v - 1 for v in values], "close": values,
         "volume": [1000.0] * len(values)},
        index=pd.DatetimeIndex(pd.to_datetime(dates)),
    )


class TestDataStore:
    def test_write_and_read(self, tmp_path: Path):
        store = DataStore(base_dir=str(tmp_path))
        df = _make_df()
        store.write("TEST", df, append=False)
        loaded = store.read("TEST")
        assert len(loaded) == len(df)
        assert loaded.iloc[-1]["close"] == df.iloc[-1]["close"]

    def test_append_merge_dedup(self, tmp_path: Path):
        store = DataStore(base_dir=str(tmp_path))
        df1 = _make_df()
        store.write("TEST", df1, append=False)

        # Append overlapping data (same dates, different values)
        df2 = _make_df(values=list(range(200, 205)))
        store.write("TEST", df2, append=True)

        loaded = store.read("TEST")
        assert len(loaded) == len(df1)  # Deduped to same count
        # Should keep latest values (from df2)
        assert loaded.iloc[-1]["close"] == 204

    def test_missing_file_returns_empty(self, tmp_path: Path):
        store = DataStore(base_dir=str(tmp_path))
        loaded = store.read("NONEXISTENT")
        assert loaded.empty

    def test_version_tracking(self, tmp_path: Path):
        store = DataStore(base_dir=str(tmp_path))
        df = _make_df()
        store.write("TEST", df, append=False)
        versions = store.get_versions("TEST")
        assert len(versions) >= 1
        ver = store.get_latest_version("TEST", Frequency.DAILY)
        assert ver is not None
        assert ver["symbol"] == "TEST"
        assert "hash" in ver

    def test_version_filtered_by_symbol(self, tmp_path: Path):
        store = DataStore(base_dir=str(tmp_path))
        store.write("A", _make_df(), append=False)
        store.write("B", _make_df(), append=False)
        a_versions = store.get_versions("A")
        assert all(v["symbol"] == "A" for v in a_versions)

    def test_freshness_check(self, tmp_path: Path):
        store = DataStore(base_dir=str(tmp_path))
        df = _make_df()
        store.write("OLD", df, append=False)
        # Data ends at 2024-01-05 (~2 years ago), should fail a 24h threshold
        is_fresh = store.check_freshness("OLD", max_age_hours=24)
        assert is_fresh is False
        # But passes a very large threshold
        is_fresh_wide = store.check_freshness("OLD", max_age_hours=365*24*3)
        assert is_fresh_wide is True

    def test_write_batch(self, tmp_path: Path):
        store = DataStore(base_dir=str(tmp_path))
        results = [
            ("A", _make_df(), Frequency.DAILY),
            ("B", _make_df(values=list(range(200, 205))), Frequency.DAILY),
        ]
        store.write_batch(results, append=False)
        assert not store.read("A").empty
        assert not store.read("B").empty

    def test_clickhouse_sync_requires_client(self, tmp_path: Path):
        store = DataStore(base_dir=str(tmp_path))
        result = store.sync_to_clickhouse("TEST", _make_df())
        assert result["synced"] is False
        assert result["reason"] == "clickhouse_client_not_configured"

    def test_clickhouse_sync_uses_injected_client(self, tmp_path: Path):
        class FakeClient:
            def __init__(self):
                self.query = ""
                self.df = None

            def insert_dataframe(self, query: str, df: pd.DataFrame):
                self.query = query
                self.df = df

        client = FakeClient()
        store = DataStore(base_dir=str(tmp_path), clickhouse_client=client)
        result = store.sync_to_clickhouse("TEST", _make_df())

        assert result["synced"] is True
        assert result["rows"] == 5
        assert client.query == "INSERT INTO market_data VALUES"
        assert "symbol" in client.df.columns
        assert client.df["symbol"].iloc[0] == "TEST"
