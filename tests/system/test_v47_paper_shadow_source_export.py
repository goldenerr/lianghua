import importlib.util
import sys
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


def _source_export() -> dict:
    return {
        "date": "2026-01-02",
        "account": {
            "account_id": "paper-v40",
            "account_type": "paper",
            "auto_trade_enabled": False,
            "live_order_submission_allowed": False,
        },
        "returns": {"paper_daily_return": 0.001, "backtest_daily_return": 0.0011},
        "costs": {
            "actual_slippage_bps": 2.0,
            "expected_slippage_bps": 3.0,
            "actual_cost_bps": 4.0,
            "expected_cost_bps": 5.0,
        },
        "risk": {"risk_capacity_violations": 0, "invariant_violations": 0},
        "refs": {
            "archive_ref": "worm://paper-shadow/2026-01-02",
            "market_data_ref": "provider://paper-shadow/feed/2026-01-02",
            "position_snapshot_ref": "broker://paper-shadow/position/2026-01-02",
            "order_fill_log_ref": "broker://paper-shadow/fills/2026-01-02",
            "paper_trading_90d": "paper://v40-shadow/2026-q1",
        },
        "orders": [
            {
                "client_order_id": "v40-20260102-0001",
                "symbol": "600000",
                "side": "BUY",
                "quantity": 100,
            }
        ],
        "fills": [
            {
                "client_order_id": "v40-20260102-0001",
                "symbol": "600000",
                "side": "BUY",
                "quantity": 100,
                "price": 10.0,
            }
        ],
        "positions": [
            {
                "symbol": "600000",
                "previous_quantity": 0,
                "fill_delta_quantity": 100,
                "end_quantity": 100,
            }
        ],
        "market_data": {
            "ref": "provider://paper-shadow/feed/2026-01-02",
            "symbols": ["600000"],
            "asof": "2026-01-02T15:00:00+08:00",
        },
        "audit": {
            "audit_events": [
                {"event_type": "paper_order", "trace_id": "v40-1"},
                {"event_type": "paper_fill", "trace_id": "v40-1"},
            ]
        },
        "controls": {
            "auto_trade_enabled": False,
            "live_order_submission_allowed": False,
        },
    }


def test_valid_source_export_can_build_v45_report_without_warnings() -> None:
    v47 = _load_module("validate_v47_paper_shadow_source_export", "validate_v47_paper_shadow_source_export.py")
    v45 = _load_module("ingest_v45_paper_shadow_daily_report_v47_test", "ingest_v45_paper_shadow_daily_report.py")

    source = _source_export()
    result = v47.validate_source_export(source)
    report = v47._build_daily_report(source)
    row, warnings = v45.canonicalize_daily_report(report)

    assert result["source_export_valid"] is True
    assert result["production_ready"] is False
    assert result["blockers"] == []
    assert result["metrics"]["order_count"] == 1
    assert result["metrics"]["fill_count"] == 1
    assert result["metrics"]["audit_event_count"] == 2
    assert row["date"] == "2026-01-02"
    assert row["audit_event_count"] == 2
    assert warnings == []
    assert report["external_refs"]["paper_trading_90d"] == "paper://v40-shadow/2026-q1"


def test_live_permissions_and_sensitive_material_are_rejected() -> None:
    v47 = _load_module("validate_v47_paper_shadow_source_export_sensitive", "validate_v47_paper_shadow_source_export.py")
    source = _source_export()
    source["controls"]["live_order_submission_allowed"] = True
    source["account"]["auto_trade_enabled"] = True
    source["provider_token"] = "Bearer abcdefghijklmnopqrstuvwxyz"

    result = v47.validate_source_export(source)

    assert result["source_export_valid"] is False
    assert any("敏感字段" in blocker for blocker in result["blockers"])
    assert any("live_order_submission_allowed 必须为 false" in blocker for blocker in result["blockers"])
    assert any("auto_trade_enabled 必须为 false" in blocker for blocker in result["blockers"])


def test_fill_without_matching_order_and_overfill_are_rejected() -> None:
    v47 = _load_module("validate_v47_paper_shadow_source_export_fill", "validate_v47_paper_shadow_source_export.py")
    source = _source_export()
    source["fills"] = [
        {
            "client_order_id": "missing-order",
            "symbol": "600000",
            "side": "BUY",
            "quantity": 10,
            "price": 10.0,
        },
        {
            "client_order_id": "v40-20260102-0001",
            "symbol": "600000",
            "side": "BUY",
            "quantity": 101,
            "price": 10.0,
        },
    ]

    result = v47.validate_source_export(source)

    assert result["source_export_valid"] is False
    assert any("找不到对应订单" in blocker for blocker in result["blockers"])
    assert any("订单成交数量超过委托数量" in blocker for blocker in result["blockers"])


def test_position_reconciliation_mismatch_is_rejected() -> None:
    v47 = _load_module("validate_v47_paper_shadow_source_export_position", "validate_v47_paper_shadow_source_export.py")
    source = _source_export()
    source["positions"][0]["fill_delta_quantity"] = 90
    source["positions"][0]["end_quantity"] = 95

    result = v47.validate_source_export(source)

    assert result["source_export_valid"] is False
    assert any("fill_delta_quantity 与成交净变化不一致" in blocker for blocker in result["blockers"])
    assert any("previous + fill_delta != end_quantity" in blocker for blocker in result["blockers"])


def test_market_data_and_audit_hash_shape_are_fail_closed() -> None:
    v47 = _load_module("validate_v47_paper_shadow_source_export_market", "validate_v47_paper_shadow_source_export.py")
    source = _source_export()
    source["market_data"]["symbols"] = []
    source["audit"] = {
        "audit_hash": "0" * 64,
        "audit_event_count": "not-an-int",
    }

    result = v47.validate_source_export(source)

    assert result["source_export_valid"] is False
    assert any("行情导出缺少品种" in blocker for blocker in result["blockers"])
    assert any("audit_event_count 必须是数字" in blocker for blocker in result["blockers"])
    assert any("audit.audit_event_count 必须大于 0" in blocker for blocker in result["blockers"])
