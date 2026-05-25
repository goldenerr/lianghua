"""Config-driven regulatory reporting and append-only local archive adapter."""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Literal
from xml.etree.ElementTree import Element, SubElement, tostring

import yaml
from pydantic import BaseModel, ConfigDict, Field

from quant_trading.core.audit import AuditBus

from .compliance import Jurisdiction, TaxCalculator, TaxRule

UTC = timezone.utc
ReportFormat = Literal["json", "xml", "csv", "pdf"]


class RegulatoryTemplate(BaseModel):
    """Validated reporting rule for one jurisdiction."""

    model_config = ConfigDict(frozen=True)
    jurisdiction: Jurisdiction
    report_types: list[str] = Field(min_length=1)
    large_trade_threshold: float = Field(gt=0)
    retention_years: int = Field(default=7, ge=7, le=30)
    output_formats: list[ReportFormat] = Field(default=["xml", "pdf"], min_length=1)
    stamp_duty_rate: float = Field(default=0.0, ge=0, le=1)
    capital_gains_rate: float = Field(default=0.0, ge=0, le=1)
    maker_fee_rate: float = Field(default=0.0, ge=0, le=1)
    taker_fee_rate: float = Field(default=0.0, ge=0, le=1)

    def tax_rule(self) -> TaxRule:
        return TaxRule(
            stamp_duty_rate=self.stamp_duty_rate,
            capital_gains_rate=self.capital_gains_rate,
            maker_fee_rate=self.maker_fee_rate,
            taker_fee_rate=self.taker_fee_rate,
        )


class RegulatoryConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    active_jurisdiction: Jurisdiction
    templates: dict[str, RegulatoryTemplate]
    require_approval_for_mode_change: bool = True

    @classmethod
    def from_yaml(cls, path: str | Path) -> RegulatoryConfig:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.model_validate(document)

    def template(self, jurisdiction: Jurisdiction | None = None) -> RegulatoryTemplate:
        target = (jurisdiction or self.active_jurisdiction).value
        if target not in self.templates:
            raise ValueError(f"missing regulatory template for jurisdiction: {target}")
        return self.templates[target]


@dataclass(frozen=True)
class TradeRecord:
    order_id: str
    symbol: str
    side: str
    notional: float
    realized_pnl: float = 0.0
    liquidity: str = "taker"


@dataclass(frozen=True)
class RiskComplianceCheck:
    check_name: str
    passed: bool
    measured: float
    limit: float


@dataclass(frozen=True)
class RegulatoryReport:
    report_id: str
    report_type: str
    jurisdiction: Jurisdiction
    report_date: date
    payload: dict
    retention_years: int
    generated_at: str

    def to_dict(self) -> dict:
        return {
            "report_id": self.report_id,
            "report_type": self.report_type,
            "jurisdiction": self.jurisdiction.value,
            "report_date": self.report_date.isoformat(),
            "payload": self.payload,
            "retention_years": self.retention_years,
            "generated_at": self.generated_at,
        }


# Compatibility alias for callers importing the former light-weight class.
ComplianceReport = RegulatoryReport


class ComplianceReportingEngine:
    """Create daily, large-trade, and risk compliance reports from one rule set."""

    def __init__(self, config: RegulatoryConfig, audit_bus: AuditBus | None = None) -> None:
        self.config = config
        self.active_jurisdiction = config.active_jurisdiction
        self.audit_bus = audit_bus or AuditBus()

    def change_jurisdiction(self, target: Jurisdiction, approved_by: str = "", approval_ref: str = "") -> None:
        if self.config.require_approval_for_mode_change and (not approved_by.strip() or not approval_ref.strip()):
            raise PermissionError("regulatory mode change requires compliance approval")
        self.config.template(target)
        previous = self.active_jurisdiction
        self.active_jurisdiction = target
        self.audit_bus.record(
            "regulatory_mode_changed",
            "compliance",
            {"from": previous.value, "to": target.value, "approved_by": approved_by, "approval_ref": approval_ref},
        )

    def generate_daily_reports(
        self,
        report_date: date,
        trades: list[TradeRecord],
        risk_checks: list[RiskComplianceCheck],
    ) -> list[RegulatoryReport]:
        template = self.config.template(self.active_jurisdiction)
        tax = TaxCalculator(self.active_jurisdiction, template.tax_rule())
        totals = {
            "total_trades": len(trades),
            "total_notional": round(sum(trade.notional for trade in trades), 2),
            "stamp_duty_paid": round(sum(tax.stamp_duty(t.notional) for t in trades if t.side.lower() == "sell"), 2),
            "execution_fees": round(sum(tax.execution_fee(t.notional, t.liquidity) for t in trades), 2),
            "capital_gains_tax": round(sum(tax.capital_gains(t.realized_pnl) for t in trades), 2),
        }
        large_trades = [asdict(trade) for trade in trades if trade.notional >= template.large_trade_threshold]
        failed_checks = [asdict(check) for check in risk_checks if not check.passed]
        data = {
            "daily_trade": {**totals, "orders": [asdict(trade) for trade in trades]},
            "large_trade": {"threshold": template.large_trade_threshold, "trades": large_trades},
            "risk_compliance": {"checks": [asdict(check) for check in risk_checks], "failed_checks": failed_checks},
        }
        reports = [
            RegulatoryReport(
                report_id=f"{self.active_jurisdiction.value}-{report_date.isoformat()}-{report_type}",
                report_type=report_type,
                jurisdiction=self.active_jurisdiction,
                report_date=report_date,
                payload=data[report_type],
                retention_years=template.retention_years,
                generated_at=datetime.now(UTC).isoformat(),
            )
            for report_type in template.report_types
            if report_type in data
        ]
        self.audit_bus.record(
            "compliance_reports_generated",
            "compliance",
            {"jurisdiction": self.active_jurisdiction.value, "date": report_date.isoformat(), "report_ids": [r.report_id for r in reports]},
        )
        return reports

    def export(self, report: RegulatoryReport, format_name: ReportFormat) -> bytes:
        template = self.config.template(report.jurisdiction)
        if format_name not in template.output_formats:
            raise ValueError(f"format not enabled for jurisdiction: {format_name}")
        if format_name == "json":
            return json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        if format_name == "xml":
            return _to_xml(report)
        if format_name == "csv":
            return _to_csv(report)
        return _to_pdf(report)


class ImmutableReportArchive:
    """Write-once local adapter; production still requires an external WORM backend."""

    def __init__(self, root: str | Path, audit_bus: AuditBus | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.audit_bus = audit_bus or AuditBus()
        self._chain_path = self.root / "archive-chain.jsonl"

    def archive(self, report: RegulatoryReport, document: bytes, suffix: str) -> Path:
        safe_suffix = suffix.lstrip(".")
        path = self.root / f"{report.report_id}.{safe_suffix}"
        try:
            with path.open("xb") as stream:
                stream.write(document)
        except FileExistsError as exc:
            raise FileExistsError("archived compliance reports cannot be overwritten") from exc
        previous_hash = self._last_hash()
        entry = {
            "report_id": report.report_id,
            "file": path.name,
            "sha256": hashlib.sha256(document).hexdigest(),
            "previous_hash": previous_hash,
            "retention_years": report.retention_years,
            "archived_at": datetime.now(UTC).isoformat(),
        }
        entry["hash"] = _hash_entry(entry)
        with self._chain_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=True, sort_keys=True) + "\n")
        self.audit_bus.record("compliance_report_archived", "compliance", {"report_id": report.report_id, "sha256": entry["sha256"]})
        return path

    def verify_integrity(self) -> bool:
        previous_hash = "GENESIS"
        for entry in self._entries():
            path = self.root / entry["file"]
            if entry["previous_hash"] != previous_hash or not path.is_file():
                return False
            if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                return False
            stored_hash = entry["hash"]
            candidate = dict(entry)
            candidate.pop("hash")
            if stored_hash != _hash_entry(candidate):
                return False
            previous_hash = stored_hash
        return True

    def _entries(self) -> list[dict]:
        if not self._chain_path.exists():
            return []
        return [json.loads(line) for line in self._chain_path.read_text(encoding="utf-8").splitlines() if line]

    def _last_hash(self) -> str:
        entries = self._entries()
        return entries[-1]["hash"] if entries else "GENESIS"


def _hash_entry(entry: dict) -> str:
    return hashlib.sha256(json.dumps(entry, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _to_xml(report: RegulatoryReport) -> bytes:
    root = Element("regulatoryReport", {"id": report.report_id, "type": report.report_type})
    SubElement(root, "jurisdiction").text = report.jurisdiction.value
    SubElement(root, "reportDate").text = report.report_date.isoformat()
    SubElement(root, "retentionYears").text = str(report.retention_years)
    payload = SubElement(root, "payload")
    payload.text = json.dumps(report.payload, ensure_ascii=False, sort_keys=True)
    return tostring(root, encoding="utf-8", xml_declaration=True)


def _to_csv(report: RegulatoryReport) -> bytes:
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["report_id", "report_type", "jurisdiction", "report_date", "payload"])
    writer.writerow([report.report_id, report.report_type, report.jurisdiction.value, report.report_date.isoformat(), json.dumps(report.payload, ensure_ascii=False, sort_keys=True)])
    return output.getvalue().encode("utf-8")


def _to_pdf(report: RegulatoryReport) -> bytes:
    text = f"{report.report_id} | {report.jurisdiction.value} | {report.report_type} | {report.report_date.isoformat()}"
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 11 Tf 50 760 Td ({escaped}) Tj ET".encode("latin-1", errors="replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    body = b"%PDF-1.4\n"
    offsets = [0]
    for idx, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{idx} 0 obj\n".encode() + obj + b"\nendobj\n"
    start = len(body)
    body += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    body += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    body += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    return body
