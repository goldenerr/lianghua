import importlib.util
import sys
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def _load_module():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "audit_v29_20y_data_coverage",
        SCRIPTS / "audit_v29_20y_data_coverage.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_price_file(path: Path, dates: pd.DatetimeIndex) -> None:
    frame = pd.DataFrame(
        {
            "close": [10.0 + index for index in range(len(dates))],
            "volume": [1000.0] * len(dates),
            "amount": [10000.0] * len(dates),
        },
        index=dates,
    )
    frame.to_parquet(path)


def test_20y_audit_distinguishes_diagnostic_from_production_claim(tmp_path) -> None:
    module = _load_module()
    data_dir = tmp_path / "parquet"
    data_dir.mkdir()
    universe = tmp_path / "universe.json"
    universe.write_text('["000001", "000002", "000003", "000004"]', encoding="utf-8")
    full_dates = pd.bdate_range("2006-01-02", "2006-01-31")
    late_dates = pd.bdate_range("2006-01-16", "2006-01-31")
    _write_price_file(data_dir / "000001.parquet", full_dates)
    _write_price_file(data_dir / "000002.parquet", full_dates)
    _write_price_file(data_dir / "000003.parquet", late_dates)

    config = module.AuditConfig(
        universe=universe,
        data_dir=data_dir,
        start=module._parse_date("20060102"),
        end=module._parse_date("20060131"),
        warmup_days=5,
        min_rows_per_year=10,
        production_min_coverage_ratio=0.95,
        diagnostic_min_symbols=2,
    )

    payload, rows, daily = module.audit_coverage(module.read_codes(universe), config)

    assert len(rows) == 4
    assert payload["summary"]["strict_20y_quality_symbols"] == 2
    assert payload["assessment"]["can_run_20y_diagnostic_backtest"] is True
    assert payload["assessment"]["can_claim_20y_full_universe_production_validation"] is False
    assert daily["warmup_eligible_count"].max() >= 2


def test_20y_audit_writes_three_outputs_without_secret_like_payloads(tmp_path) -> None:
    module = _load_module()
    data_dir = tmp_path / "parquet"
    data_dir.mkdir()
    universe = tmp_path / "universe.json"
    universe.write_text('["000001"]', encoding="utf-8")
    _write_price_file(data_dir / "000001.parquet", pd.bdate_range("2006-01-02", "2006-01-31"))
    config = module.AuditConfig(
        universe=universe,
        data_dir=data_dir,
        start=module._parse_date("20060102"),
        end=module._parse_date("20060131"),
        warmup_days=3,
        min_rows_per_year=10,
        production_min_coverage_ratio=0.95,
        diagnostic_min_symbols=1,
    )
    payload, rows, daily = module.audit_coverage(module.read_codes(universe), config)
    output_json = tmp_path / "audit.json"
    symbol_csv = tmp_path / "symbols.csv"
    daily_csv = tmp_path / "daily.csv"

    module.write_outputs(
        payload,
        rows,
        daily,
        output_json=output_json,
        symbol_csv=symbol_csv,
        daily_csv=daily_csv,
    )

    assert output_json.exists()
    assert symbol_csv.exists()
    assert daily_csv.exists()
    assert "token" not in output_json.read_text(encoding="utf-8").lower()
