#!/usr/bin/env python3
"""Watchdog for V49/V45 paper-shadow evidence accumulation.

This does not fabricate evidence or relax V42 thresholds. It checks whether the
real-market paper-shadow ledger is accumulating and whether recent runtime files
remain internally consistent. In --alert-only mode it stays silent unless the
pipeline needs attention, so it is safe for a no_agent Hermes cron job.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from _paths import PROJECT_DIR, RESULTS_DIR

RUNTIME_DIR = PROJECT_DIR / "data" / "runtime" / "v49_v45"
DEFAULT_LEDGER = RUNTIME_DIR / "quant_v45_paper_shadow_ledger.json"
DEFAULT_DAILY_REPORT_DIR = RUNTIME_DIR / "daily_reports"
DEFAULT_SOURCE_EXPORT_DIR = RUNTIME_DIR / "source_exports"
DEFAULT_OUTPUT = RESULTS_DIR / "quant_v49_v45_paper_evidence_health.json"
MIN_CALENDAR_DAYS = 90
MIN_REPORT_DAYS = 60


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger-json", default=str(DEFAULT_LEDGER))
    parser.add_argument("--daily-report-dir", default=str(DEFAULT_DAILY_REPORT_DIR))
    parser.add_argument("--source-export-dir", default=str(DEFAULT_SOURCE_EXPORT_DIR))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--as-of-date", default=date.today().isoformat())
    parser.add_argument("--max-stale-calendar-days", type=int, default=5)
    parser.add_argument("--min-calendar-days", type=int, default=MIN_CALENDAR_DAYS)
    parser.add_argument("--min-report-days", type=int, default=MIN_REPORT_DAYS)
    parser.add_argument(
        "--alert-only",
        action="store_true",
        help="Print only when health_status is not ok; useful for no_agent cron.",
    )
    return parser.parse_args()


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"JSON must be an object: {path}")
    return data


def _parse_iso_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def build_health_report(
    *,
    ledger_path: Path,
    daily_report_dir: Path,
    source_export_dir: Path,
    as_of: date,
    max_stale_calendar_days: int,
    min_calendar_days: int,
    min_report_days: int,
    generated_at: datetime,
) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []
    ledger: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    if not ledger_path.exists():
        blockers.append(f"missing V45 runtime ledger: {ledger_path}")
    else:
        ledger = _load_json(ledger_path)
        raw_rows = ledger.get("daily_reports", [])
        if not isinstance(raw_rows, list):
            blockers.append("ledger.daily_reports must be a list")
        else:
            rows = [row for row in raw_rows if isinstance(row, dict)]
            if len(rows) != len(raw_rows):
                blockers.append("ledger.daily_reports contains non-object entries")

    parsed_dates: list[date] = []
    seen: set[date] = set()
    for idx, row in enumerate(rows):
        report_date = _parse_iso_date(row.get("date"))
        if report_date is None:
            blockers.append(f"daily_reports[{idx}].date invalid")
            continue
        if report_date in seen:
            blockers.append(f"duplicate daily report date: {report_date.isoformat()}")
        seen.add(report_date)
        parsed_dates.append(report_date)
        if bool(row.get("auto_trade_enabled")) or bool(row.get("live_order_submission_allowed")):
            blockers.append(f"{report_date.isoformat()}: live trading flags must remain false")
        if int(row.get("risk_capacity_violations", 0) or 0) != 0:
            blockers.append(f"{report_date.isoformat()}: risk_capacity_violations must be zero")
        if int(row.get("invariant_violations", 0) or 0) != 0:
            blockers.append(f"{report_date.isoformat()}: invariant_violations must be zero")
        report_file = daily_report_dir / f"{report_date.isoformat()}.json"
        source_file = source_export_dir / f"{report_date.isoformat()}.json"
        if not report_file.exists():
            blockers.append(f"{report_date.isoformat()}: missing runtime daily report file")
        if not source_file.exists():
            blockers.append(f"{report_date.isoformat()}: missing runtime source export file")

    parsed_dates.sort()
    report_days = len(parsed_dates)
    first_date = parsed_dates[0] if parsed_dates else None
    latest_date = parsed_dates[-1] if parsed_dates else None
    calendar_span = (latest_date - first_date).days + 1 if first_date and latest_date else 0
    latest_age = (as_of - latest_date).days if latest_date else None
    if report_days == 0:
        warnings.append("no paper-shadow report days accumulated yet")
    if latest_date is not None and latest_age is not None and latest_age > max_stale_calendar_days:
        blockers.append(
            f"latest paper-shadow report is stale: {latest_date.isoformat()} age={latest_age}d > {max_stale_calendar_days}d"
        )
    if latest_date is not None and latest_date > as_of:
        blockers.append(
            f"latest paper-shadow report date is in the future: {latest_date.isoformat()}"
        )

    progress = {
        "report_days": report_days,
        "calendar_days": calendar_span,
        "first_date": first_date.isoformat() if first_date else None,
        "latest_date": latest_date.isoformat() if latest_date else None,
        "latest_age_calendar_days": latest_age,
        "required_report_days": min_report_days,
        "required_calendar_days": min_calendar_days,
        "report_day_progress": round(report_days / min_report_days, 4) if min_report_days else 0.0,
        "calendar_day_progress": (
            round(calendar_span / min_calendar_days, 4) if min_calendar_days else 0.0
        ),
        "paper_evidence_threshold_met": report_days >= min_report_days
        and calendar_span >= min_calendar_days,
    }
    health_status = "blocked" if blockers else "ok"
    return {
        "ts": generated_at.isoformat(),
        "version": "V49/V45-paper-evidence-health-watchdog",
        "research_only": False,
        "production_ready": False,
        "ledger_path": str(ledger_path),
        "as_of_date": as_of.isoformat(),
        "health_status": health_status,
        "progress": progress,
        "blockers": blockers,
        "warnings": warnings,
        "next_required_work": [
            "Keep the V49/V45 daily cron enabled after A-share close.",
            "Do not backfill or fabricate missing report days; investigate stale/missing runtime files instead.",
            "Run V42 strict evidence gate only after >=90 calendar days and >=60 report days are naturally accumulated.",
        ],
    }


def main() -> None:
    args = _parse_args()
    report = build_health_report(
        ledger_path=Path(args.ledger_json),
        daily_report_dir=Path(args.daily_report_dir),
        source_export_dir=Path(args.source_export_dir),
        as_of=date.fromisoformat(args.as_of_date),
        max_stale_calendar_days=args.max_stale_calendar_days,
        min_calendar_days=args.min_calendar_days,
        min_report_days=args.min_report_days,
        generated_at=datetime.now(timezone.utc),
    )
    output = Path(args.output_json)
    output.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    output.write_text(rendered, encoding="utf-8")
    if args.alert_only and report["health_status"] == "ok":
        return
    print(rendered, end="")


if __name__ == "__main__":
    main()
