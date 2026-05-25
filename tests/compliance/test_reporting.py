from datetime import date

import pytest
from quant_trading.compliance.compliance import (
    Jurisdiction,
    TaxLossPosition,
    suggest_tax_loss_harvesting,
)
from quant_trading.compliance.reporting import (
    ComplianceReportingEngine,
    ImmutableReportArchive,
    RegulatoryConfig,
    RiskComplianceCheck,
    TradeRecord,
)
from quant_trading.core.audit import AuditBus


def _config() -> RegulatoryConfig:
    return RegulatoryConfig.from_yaml("config/compliance.yaml")


def test_cn_daily_large_trade_and_risk_reports_follow_template() -> None:
    engine = ComplianceReportingEngine(_config())
    trades = [
        TradeRecord("buy-1", "600000", "buy", 2_000_000.0),
        TradeRecord("sell-1", "600001", "sell", 1_500_000.0, realized_pnl=30_000.0),
    ]
    checks = [
        RiskComplianceCheck("leverage", True, 1.0, 2.0),
        RiskComplianceCheck("position_limit", False, 0.25, 0.20),
    ]

    reports = engine.generate_daily_reports(date(2026, 5, 25), trades, checks)
    by_type = {report.report_type: report for report in reports}
    assert set(by_type) == {"daily_trade", "large_trade", "risk_compliance"}
    assert by_type["daily_trade"].payload["stamp_duty_paid"] == 750.0
    assert len(by_type["large_trade"].payload["trades"]) == 2
    assert by_type["risk_compliance"].payload["failed_checks"][0]["check_name"] == "position_limit"


def test_regulatory_mode_change_requires_approval_and_applies_crypto_fees() -> None:
    audit = AuditBus()
    engine = ComplianceReportingEngine(_config(), audit_bus=audit)
    with pytest.raises(PermissionError, match="approval"):
        engine.change_jurisdiction(Jurisdiction.CRYPTO)

    engine.change_jurisdiction(Jurisdiction.CRYPTO, "compliance_officer", "APP-2026-001")
    report = engine.generate_daily_reports(
        date(2026, 5, 25),
        [TradeRecord("crypto-1", "BTC/USDT", "buy", 100_000, liquidity="maker")],
        [],
    )[0]
    assert report.payload["execution_fees"] == 20.0
    assert audit.query("regulatory_mode_changed")[0]["payload"]["approval_ref"] == "APP-2026-001"


def test_xml_csv_pdf_exports_archive_once_and_detect_tampering(tmp_path) -> None:
    engine = ComplianceReportingEngine(_config())
    report = engine.generate_daily_reports(date(2026, 5, 25), [], [])[0]
    xml = engine.export(report, "xml")
    csv_document = engine.export(report, "csv")
    pdf = engine.export(report, "pdf")
    assert xml.startswith(b"<?xml")
    assert report.report_id.encode() in csv_document
    assert pdf.startswith(b"%PDF-")

    archive = ImmutableReportArchive(tmp_path)
    stored = archive.archive(report, xml, "xml")
    assert archive.verify_integrity() is True
    with pytest.raises(FileExistsError, match="cannot be overwritten"):
        archive.archive(report, xml, "xml")
    stored.write_bytes(b"changed outside archive API")
    assert archive.verify_integrity() is False


def test_tax_loss_suggestions_exclude_wash_sale_candidates() -> None:
    positions = [
        TaxLossPosition("LOSS_OK", 10, 100.0, 70.0, date(2025, 1, 1)),
        TaxLossPosition("WASH", 10, 100.0, 60.0, date(2025, 1, 1)),
        TaxLossPosition("WIN", 10, 100.0, 120.0, date(2025, 1, 1)),
    ]
    suggestions = suggest_tax_loss_harvesting(
        positions,
        recent_buys={"WASH": date(2026, 5, 12)},
        as_of=date(2026, 5, 25),
        minimum_loss=100,
    )
    assert suggestions == [{"symbol": "LOSS_OK", "realizable_loss": 300.0}]
