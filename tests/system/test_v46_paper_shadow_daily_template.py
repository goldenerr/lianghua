import importlib.util
import json
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
        "audit": {"audit_events": [{"event_type": "paper_order"}]},
        "controls": {
            "auto_trade_enabled": False,
            "live_order_submission_allowed": False,
        },
    }


def test_template_and_example_are_explicitly_not_ingestable() -> None:
    v46 = _load_module("generate_v46_paper_shadow_daily_template", "generate_v46_paper_shadow_daily_template.py")
    v45 = _load_module("ingest_v45_paper_shadow_daily_report_v46_test", "ingest_v45_paper_shadow_daily_report.py")

    template = v46.build_template()
    example = v46.build_example()

    assert template["template_only"] is True
    assert example["example_only"] is True
    for payload in (template, example):
        try:
            v45.canonicalize_daily_report(payload)
        except ValueError as exc:
            assert "模板或示例日报不能写入" in str(exc)
        else:
            raise AssertionError("template/example reports must not be ingestable")


def test_adapter_converts_normalized_source_export_to_v45_report() -> None:
    v46 = _load_module("generate_v46_paper_shadow_daily_template", "generate_v46_paper_shadow_daily_template.py")
    v45 = _load_module("ingest_v45_paper_shadow_daily_report_v46_adapter", "ingest_v45_paper_shadow_daily_report.py")

    report = v46.build_daily_report_from_export(_source_export())
    row, warnings = v45.canonicalize_daily_report(report)

    assert "template_only" not in report
    assert "example_only" not in report
    assert row["date"] == "2026-01-02"
    assert row["audit_event_count"] == 1
    assert warnings == []
    assert report["external_refs"]["paper_trading_90d"] == "paper://v40-shadow/2026-q1"


def test_v46_outputs_chinese_report_and_safe_json_files(tmp_path: Path) -> None:
    v46 = _load_module("generate_v46_paper_shadow_daily_template", "generate_v46_paper_shadow_daily_template.py")
    template = tmp_path / "template.json"
    example = tmp_path / "example.json"
    mapping = tmp_path / "mapping.json"
    report = tmp_path / "report.md"
    v46._write_json(template, v46.build_template())
    v46._write_json(example, v46.build_example())
    v46._write_json(mapping, v46.build_mapping())
    v46._write_report(report, template=template, example=example, mapping=mapping, daily_output=None)

    template_payload = json.loads(template.read_text(encoding="utf-8"))
    example_payload = json.loads(example.read_text(encoding="utf-8"))
    mapping_payload = json.loads(mapping.read_text(encoding="utf-8"))
    report_text = report.read_text(encoding="utf-8")

    assert template_payload["template_only"] is True
    assert example_payload["example_only"] is True
    assert mapping_payload["production_ready"] is False
    assert "状态：模板和适配器已生成" in report_text
    assert "任何令牌、接口密钥、密码或私钥" in report_text
