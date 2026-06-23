import importlib.util
import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"


def _load_module(name: str, filename: str):
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _row(
    *,
    capital: float,
    sharpe: float,
    executable: bool = True,
    minimum: bool = False,
) -> dict:
    return {
        "name": f"v33_small_account_{int(capital)}",
        "scenario": {
            "capital": capital,
            "max_positions": 5,
            "rank_mode": "blend",
            "gross_profile": "small_balanced",
            "rebalance_freq": 10,
            "lot_size": 100,
            "candidate_pool_multiplier": 6,
            "max_per_industry": 2,
            "per_position_budget_buffer": 1.2,
        },
        "cost": {
            "commission_rate": 0.00025,
            "min_commission": 5.0,
            "stamp_duty_rate": 0.0005,
            "slippage_bps": 5.0,
        },
        "small_account_executable_gate_passes": executable,
        "small_account_minimum_gate_passes": minimum,
        "full": {
            "sharpe_ratio": sharpe,
            "annual_return": 0.06,
            "annual_volatility": 0.10,
            "max_drawdown": -0.22,
            "win_rate": 0.53,
            "avg_holdings": 5.0,
            "avg_stock_exposure": 0.18,
            "fees_pct_initial_capital": 0.45,
        },
        "wf": {"avg_oos_sharpe": 0.2, "folds": [{"oos": 0.1}, {"oos": 0.3}]},
    }


def _v33() -> dict:
    return {
        "version": "V33-test",
        "production_ready": False,
        "results": [
            _row(capital=50000.0, sharpe=0.14),
            _row(capital=100000.0, sharpe=0.41),
        ],
    }


def _complete_ledger(days: int = 90) -> dict:
    start = date(2026, 1, 2)
    reports = []
    for offset in range(days):
        current = start + timedelta(days=offset)
        reports.append(
            {
                "date": current.isoformat(),
                "paper_daily_return": 0.0010,
                "backtest_daily_return": 0.0011,
                "actual_slippage_bps": 2.0,
                "expected_slippage_bps": 3.0,
                "actual_cost_bps": 4.0,
                "expected_cost_bps": 5.0,
                "risk_capacity_violations": 0,
                "invariant_violations": 0,
                "audit_hash": "a" * 64,
                "audit_event_count": 9,
                "archive_ref": f"worm://small-account/v49/{current.isoformat()}",
                "market_data_ref": f"provider://small-account/feed/{current.isoformat()}",
                "position_snapshot_ref": f"broker://small-account/position/{current.isoformat()}",
                "order_fill_log_ref": f"broker://small-account/fills/{current.isoformat()}",
                "auto_trade_enabled": False,
                "live_order_submission_allowed": False,
            }
        )
    return {
        "external_refs": {"paper_trading_90d": "paper://v49-small-account/2026-q1"},
        "daily_reports": reports,
    }


def test_v49_enters_paper_validation_but_never_production() -> None:
    module = _load_module(
        "prepare_v49_small_account_paper_validation",
        "prepare_v49_small_account_paper_validation.py",
    )

    package = module.build_package(
        _v33(),
        generated_at=datetime(2026, 6, 23, tzinfo=timezone.utc),
    )

    assert package["paper_validation_ready"] is True
    assert package["paper_evidence_ready"] is False
    assert package["production_ready"] is False
    assert package["small_live_ready"] is False
    assert package["blockers"] == []
    assert len(package["candidate_accounts"]) == 2
    assert all(
        candidate["controls"]["auto_trade_enabled"] is False
        and candidate["controls"]["live_order_submission_allowed"] is False
        and candidate["controls"]["max_live_capital_fraction"] == 0.0
        for candidate in package["candidate_accounts"]
    )
    assert package["summary"]["production_candidate_count"] == 0
    assert any("小账户股票 alpha 回测未达到" in item for item in package["production_blockers"])


def test_v49_blocks_start_when_required_small_account_candidate_is_missing_or_unexecutable() -> None:
    module = _load_module(
        "prepare_v49_small_account_paper_validation_blocked",
        "prepare_v49_small_account_paper_validation.py",
    )
    v33 = {"results": [_row(capital=50000.0, sharpe=0.1, executable=False)]}

    package = module.build_package(
        v33,
        generated_at=datetime(2026, 6, 23, tzinfo=timezone.utc),
    )

    assert package["paper_validation_ready"] is False
    assert package["production_ready"] is False
    assert any("50000 元候选未通过" in item for item in package["blockers"])
    assert any("缺少 100000 元" in item for item in package["blockers"])


def test_v49_ledger_preserves_existing_daily_reports_and_refs() -> None:
    module = _load_module(
        "prepare_v49_small_account_paper_validation_ledger",
        "prepare_v49_small_account_paper_validation.py",
    )
    package = module.build_package(_v33())
    existing = {
        "external_refs": {"paper_trading_90d": "paper://existing/ref"},
        "daily_reports": [{"date": "2026-01-02", "paper_daily_return": 0.0}],
    }

    ledger = module.build_or_update_ledger(package, existing)

    assert ledger["production_ready"] is False
    assert ledger["paper_validation_ready"] is True
    assert ledger["external_refs"] == existing["external_refs"]
    assert ledger["daily_reports"] == existing["daily_reports"]
    assert len(ledger["candidate_accounts"]) == 2


def test_v49_package_is_compatible_with_v42_paper_evidence_gate() -> None:
    v49 = _load_module(
        "prepare_v49_small_account_paper_validation_v42",
        "prepare_v49_small_account_paper_validation.py",
    )
    v42 = _load_module("validate_v42_paper_shadow_evidence_v49", "validate_v42_paper_shadow_evidence.py")
    package = v49.build_package(_v33(), generated_at=datetime(2026, 6, 23, tzinfo=timezone.utc))

    payload = v42.build_evidence_gate(
        package,
        _complete_ledger(),
        generated_at=datetime(2026, 6, 23, tzinfo=timezone.utc),
    )

    assert payload["paper_evidence_ready"] is True
    assert payload["production_ready"] is False
    assert payload["metrics"]["report_days"] == 90
    assert payload["blockers"] == []


def test_v49_cli_writes_chinese_report_and_non_secret_template(tmp_path: Path) -> None:
    research = tmp_path / "v33.json"
    output = tmp_path / "package.json"
    ledger = tmp_path / "ledger.json"
    template = tmp_path / "template.json"
    report = tmp_path / "report.md"
    research.write_text(json.dumps(_v33(), ensure_ascii=False), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "prepare_v49_small_account_paper_validation.py"),
            "--v33-research-json",
            str(research),
            "--output-json",
            str(output),
            "--ledger-json",
            str(ledger),
            "--daily-template-json",
            str(template),
            "--report-md",
            str(report),
            "--require-paper-validation-ready",
        ],
        check=True,
        cwd=PROJECT,
        capture_output=True,
        text=True,
    )

    package = json.loads(output.read_text(encoding="utf-8"))
    template_payload = json.loads(template.read_text(encoding="utf-8"))
    report_text = report.read_text(encoding="utf-8")
    assert "paper_validation_ready" in result.stdout
    assert package["paper_validation_ready"] is True
    assert package["production_ready"] is False
    assert template_payload["template_only"] is True
    assert "token" not in json.dumps(template_payload, ensure_ascii=False).lower()
    assert "实盘下单：禁止" in report_text
    assert "V49 5-10万小账户" in report_text
