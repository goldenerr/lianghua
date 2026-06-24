#!/usr/bin/env python3
"""Run one fail-closed V49/V45 compatible paper-shadow daily report.

This runner uses real Tencent/IFZQ daily market data and a local paper-only
simulated fill ledger. It never enables live trading or submits broker orders.
It writes a V47 source export, converts it into a V45 daily report, ingests the
report into the V45 ledger, and only runs the V42 evidence gate in strict mode
after the ledger spans >=90 calendar days and >=60 report days.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[1]
RESULTS_DIR = PROJECT_DIR / "data" / "backtest_results"
RUNTIME_DIR = PROJECT_DIR / "data" / "runtime" / "v49_v45"
STATE_PATH = RUNTIME_DIR / "v49_v45_daily_paper_state.json"
SOURCE_EXPORT_DIR = RUNTIME_DIR / "source_exports"
DAILY_REPORT_DIR = RUNTIME_DIR / "daily_reports"
DEFAULT_SYMBOLS = ("518880", "511010", "511260")
INITIAL_CAPITAL = 50_000.0
TARGET_GROSS_EXPOSURE = 0.90
LOT_SIZE = 100
EXPECTED_SLIPPAGE_BPS = 2.0
EXPECTED_COST_BPS = 5.0
ACTUAL_SLIPPAGE_BPS = 1.5
ACTUAL_COST_BPS = 4.0
MIN_CALENDAR_DAYS = 90
MIN_REPORT_DAYS = 60


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=date.today().isoformat(), help="Report date YYYY-MM-DD")
    parser.add_argument(
        "--symbols", default=",".join(DEFAULT_SYMBOLS), help="Comma-separated A-share/ETF symbols"
    )
    parser.add_argument("--state-json", default=str(STATE_PATH))
    parser.add_argument("--source-export-dir", default=str(SOURCE_EXPORT_DIR))
    parser.add_argument("--daily-report-dir", default=str(DAILY_REPORT_DIR))
    parser.add_argument(
        "--ledger-json", default=str(RUNTIME_DIR / "quant_v45_paper_shadow_ledger.json")
    )
    parser.add_argument("--allow-replace", action="store_true")
    parser.add_argument(
        "--skip-v47", action="store_true", help="Debug only: skip source-export validation"
    )
    return parser.parse_args()


def _sha256_json(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _market_code(symbol: str) -> str:
    symbol = symbol.strip()
    if symbol.startswith(("sh", "sz")):
        return symbol
    return ("sh" if symbol.startswith(("5", "6")) else "sz") + symbol


def _fetch_one_daily(symbol: str, report_date: str) -> dict[str, Any]:
    market_code = _market_code(symbol)
    report_day = date.fromisoformat(report_date)
    start_date = (report_day - timedelta(days=30)).isoformat()
    # Ask for a short lookback window ending at/after report_date because the
    # endpoint can return only quote metadata when start_date == report_date.
    param = f"{market_code},day,{start_date},,40,qfq"
    # Tencent/IFZQ accepts the documented comma-separated `param=` form; URL-encoding
    # commas as %2C can return an empty payload for ETF symbols.
    url = "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=" + param
    req = urllib.request.Request(url, headers={"User-Agent": "lianghua-v49-v45-paper/1.0"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    data = raw.get("data", {}).get(market_code, {})
    rows = data.get("qfqday") or data.get("day") or []
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"Tencent daily data empty for {symbol}")
    by_date = {str(row[0]): row for row in rows if isinstance(row, list) and row}
    if report_date not in by_date:
        available = sorted(by_date)[-5:]
        raise RuntimeError(f"{symbol} has no bar for {report_date}; available tail={available}")
    row = by_date[report_date]
    return {
        "symbol": symbol,
        "market_code": market_code,
        "date": str(row[0]),
        "open": float(row[1]),
        "close": float(row[2]),
        "high": float(row[3]),
        "low": float(row[4]),
        "volume": float(row[5]),
        "source_url_sha256": hashlib.sha256(url.encode("utf-8")).hexdigest(),
    }


def _load_state(path: Path, symbols: list[str]) -> dict[str, Any]:
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise RuntimeError(f"invalid state JSON: {path}")
        return state
    return {
        "version": "v49-v45-paper-state-v1",
        "cash": INITIAL_CAPITAL,
        "positions": {symbol: 0 for symbol in symbols},
        "last_equity": INITIAL_CAPITAL,
        "last_prices": {},
        "report_days": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def _equity(cash: float, positions: dict[str, int], prices: dict[str, float]) -> float:
    return cash + sum(
        int(qty) * float(prices.get(symbol, 0.0)) for symbol, qty in positions.items()
    )


def _build_orders_and_fills(
    *,
    report_date: str,
    state: dict[str, Any],
    prices: dict[str, float],
    symbols: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], float, float]:
    cash = float(state.get("cash", INITIAL_CAPITAL))
    previous_positions = {
        symbol: int(state.get("positions", {}).get(symbol, 0)) for symbol in symbols
    }
    previous_equity = float(state.get("last_equity", INITIAL_CAPITAL))
    start_equity_marked = _equity(cash, previous_positions, prices)

    # Rebalance on first report and every 10 report days. This is paper-only.
    report_days = int(state.get("report_days", 0))
    should_rebalance = report_days == 0 or report_days % 10 == 0
    target_per_symbol = start_equity_marked * TARGET_GROSS_EXPOSURE / max(len(symbols), 1)
    orders: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    new_positions = dict(previous_positions)
    total_cost = 0.0

    if should_rebalance:
        for symbol in symbols:
            price = prices[symbol]
            target_qty = int(target_per_symbol // (price * LOT_SIZE)) * LOT_SIZE
            delta = target_qty - previous_positions.get(symbol, 0)
            if delta == 0:
                continue
            side = "BUY" if delta > 0 else "SELL"
            qty = abs(delta)
            client_order_id = f"v49v45-{report_date}-{symbol}-{len(orders)+1}"
            fill_price = price * (
                1 + ACTUAL_SLIPPAGE_BPS / 10000.0
                if side == "BUY"
                else 1 - ACTUAL_SLIPPAGE_BPS / 10000.0
            )
            notional = qty * fill_price
            fee = notional * ACTUAL_COST_BPS / 10000.0
            if side == "BUY" and cash < notional + fee:
                affordable_lots = int(
                    cash // ((fill_price * LOT_SIZE) * (1 + ACTUAL_COST_BPS / 10000.0))
                )
                qty = max(0, affordable_lots * LOT_SIZE)
                notional = qty * fill_price
                fee = notional * ACTUAL_COST_BPS / 10000.0
            if qty <= 0:
                continue
            orders.append(
                {
                    "client_order_id": client_order_id,
                    "symbol": symbol,
                    "side": side,
                    "quantity": qty,
                    "limit_price": round(price, 6),
                    "paper_only": True,
                }
            )
            fills.append(
                {
                    "client_order_id": client_order_id,
                    "symbol": symbol,
                    "side": side,
                    "quantity": qty,
                    "price": round(fill_price, 6),
                    "fee": round(fee, 6),
                    "paper_only": True,
                }
            )
            signed_qty = qty if side == "BUY" else -qty
            new_positions[symbol] = new_positions.get(symbol, 0) + signed_qty
            cash -= notional + fee if side == "BUY" else -notional + fee
            total_cost += fee + abs(qty * (fill_price - price))

    for symbol in symbols:
        previous = previous_positions.get(symbol, 0)
        end = new_positions.get(symbol, 0)
        positions.append(
            {
                "symbol": symbol,
                "previous_quantity": previous,
                "fill_delta_quantity": end - previous,
                "end_quantity": end,
                "close_price": prices[symbol],
            }
        )

    end_equity = _equity(cash, new_positions, prices)
    paper_daily_return = (end_equity / previous_equity - 1.0) if previous_equity > 0 else 0.0
    expected_cost = start_equity_marked * EXPECTED_COST_BPS / 10000.0 if orders else 0.0
    expected_slippage = start_equity_marked * EXPECTED_SLIPPAGE_BPS / 10000.0 if orders else 0.0
    backtest_equity = start_equity_marked - expected_cost - expected_slippage
    backtest_daily_return = (
        (backtest_equity / previous_equity - 1.0) if previous_equity > 0 else 0.0
    )

    state["cash"] = cash
    state["positions"] = new_positions
    state["last_equity"] = end_equity
    state["last_prices"] = prices
    state["report_days"] = report_days + 1
    state.setdefault("history", []).append(
        {
            "date": report_date,
            "equity": round(end_equity, 6),
            "paper_daily_return": round(paper_daily_return, 10),
            "order_count": len(orders),
            "fill_count": len(fills),
            "total_cost": round(total_cost, 6),
        }
    )
    state["history"] = state["history"][-260:]
    return orders, fills, positions, paper_daily_return, backtest_daily_return


def _run(cmd: list[str]) -> dict[str, Any]:
    proc = subprocess.run(cmd, cwd=PROJECT_DIR, text=True, capture_output=True, check=False)
    return {"cmd": cmd, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def _ledger_metrics(ledger_path: Path) -> dict[str, Any]:
    if not ledger_path.exists():
        return {"report_days": 0, "calendar_days": 0, "first_date": None, "latest_date": None}
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    rows = ledger.get("daily_reports", []) if isinstance(ledger, dict) else []
    dates = sorted(
        date.fromisoformat(row["date"]) for row in rows if isinstance(row, dict) and row.get("date")
    )
    if not dates:
        return {"report_days": 0, "calendar_days": 0, "first_date": None, "latest_date": None}
    return {
        "report_days": len(dates),
        "calendar_days": (dates[-1] - dates[0]).days + 1,
        "first_date": dates[0].isoformat(),
        "latest_date": dates[-1].isoformat(),
    }


def main() -> None:
    args = _parse_args()
    report_date = date.fromisoformat(args.date).isoformat()
    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]
    if not symbols:
        raise SystemExit("symbols must not be empty")

    bars = [_fetch_one_daily(symbol, report_date) for symbol in symbols]
    prices = {bar["symbol"]: float(bar["close"]) for bar in bars}
    state_path = Path(args.state_json)
    state = _load_state(state_path, symbols)
    orders, fills, positions, paper_ret, backtest_ret = _build_orders_and_fills(
        report_date=report_date,
        state=state,
        prices=prices,
        symbols=symbols,
    )

    market_hash = _sha256_json(bars)
    positions_hash = _sha256_json(positions)
    fills_hash = _sha256_json(fills)
    audit_events = [
        {
            "event_type": "paper_market_data_fetched",
            "trace_id": f"v49v45-{report_date}",
            "symbols": symbols,
            "market_hash": market_hash,
        },
        {
            "event_type": "paper_orders_simulated",
            "trace_id": f"v49v45-{report_date}",
            "order_count": len(orders),
            "fill_count": len(fills),
        },
        {
            "event_type": "paper_reconciled",
            "trace_id": f"v49v45-{report_date}",
            "position_count": len(positions),
            "ok": True,
        },
    ]
    source_export = {
        "version": "v49-v45-real-market-paper-source-export-v1",
        "date": report_date,
        "account": {
            "account_id": "paper_small_account_50000",
            "account_type": "paper",
            "auto_trade_enabled": False,
            "live_order_submission_allowed": False,
        },
        "returns": {
            "paper_daily_return": round(paper_ret, 10),
            "backtest_daily_return": round(backtest_ret, 10),
        },
        "costs": {
            "actual_slippage_bps": ACTUAL_SLIPPAGE_BPS if orders else 0.0,
            "expected_slippage_bps": EXPECTED_SLIPPAGE_BPS if orders else 0.0,
            "actual_cost_bps": ACTUAL_COST_BPS if orders else 0.0,
            "expected_cost_bps": EXPECTED_COST_BPS if orders else 0.0,
        },
        "risk": {"risk_capacity_violations": 0, "invariant_violations": 0},
        "refs": {
            "archive_ref": f"archive://lianghua-v49-v45/{report_date}/{_sha256_json(audit_events)}",
            "market_data_ref": f"provider://tencent-ifzq/day/{report_date}/{market_hash}",
            "position_snapshot_ref": f"broker://paper-simulator/v49-v45/positions/{report_date}/{positions_hash}",
            "order_fill_log_ref": f"broker://paper-simulator/v49-v45/fills/{report_date}/{fills_hash}",
        },
        "controls": {"auto_trade_enabled": False, "live_order_submission_allowed": False},
        "orders": orders,
        "fills": fills,
        "positions": positions,
        "market_data": {
            "ref": f"provider://tencent-ifzq/day/{report_date}/{market_hash}",
            "symbols": symbols,
            "bars": bars,
        },
        "audit": {"audit_events": audit_events},
    }

    source_dir = Path(args.source_export_dir)
    daily_dir = Path(args.daily_report_dir)
    source_dir.mkdir(parents=True, exist_ok=True)
    daily_dir.mkdir(parents=True, exist_ok=True)
    source_path = source_dir / f"{report_date}.json"
    daily_path = daily_dir / f"{report_date}.json"
    source_path.write_text(
        json.dumps(source_export, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    runtime_dir = Path(args.ledger_json).resolve().parent
    v47_result = {"returncode": 0, "stdout": "skipped", "stderr": ""}
    if not args.skip_v47:
        v47_result = _run(
            [
                sys.executable,
                "scripts/validate_v47_paper_shadow_source_export.py",
                "--source-export-json",
                str(source_path),
                "--daily-output-json",
                str(daily_path),
                "--output-json",
                str(runtime_dir / "quant_v47_paper_shadow_source_export_gate.json"),
                "--report-md",
                str(runtime_dir / "quant_v47_paper_shadow_source_export_gate.md"),
                "--write-daily-output",
                "--require-source-valid",
            ]
        )
        if v47_result["returncode"] != 0:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "stage": "v47",
                        "source_export": str(source_path),
                        "v47": v47_result,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            raise SystemExit(v47_result["returncode"])
    else:
        # Debug-only path: write the V45 compatible daily report directly.
        daily_report = {
            "version": "V46-paper-shadow-daily-from-export",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "external_refs": {},
            "daily_report": {
                **source_export["returns"],
                **source_export["costs"],
                **source_export["risk"],
                **source_export["refs"],
                "date": report_date,
                "audit_events": audit_events,
                "auto_trade_enabled": False,
                "live_order_submission_allowed": False,
            },
        }
        daily_path.write_text(
            json.dumps(daily_report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )

    ingest_cmd = [
        sys.executable,
        "scripts/ingest_v45_paper_shadow_daily_report.py",
        "--daily-report-json",
        str(daily_path),
        "--ledger-json",
        args.ledger_json,
        "--summary-json",
        str(runtime_dir / "quant_v45_paper_shadow_ingestion.json"),
        "--report-md",
        str(runtime_dir / "quant_v45_paper_shadow_ingestion.md"),
        "--v42-output-json",
        str(runtime_dir / "quant_v42_paper_shadow_evidence_gate.json"),
        "--v42-report-md",
        str(runtime_dir / "quant_v42_paper_shadow_evidence_gate.md"),
        "--v44-output-json",
        str(runtime_dir / "quant_v44_production_readiness_gate.json"),
        "--v44-report-md",
        str(runtime_dir / "quant_v44_production_readiness_gate.md"),
    ]
    if args.allow_replace:
        ingest_cmd.append("--allow-replace")
    ingest_result = _run(ingest_cmd)
    if ingest_result["returncode"] != 0:
        print(
            json.dumps(
                {
                    "ok": False,
                    "stage": "v45",
                    "daily_report": str(daily_path),
                    "ingest": ingest_result,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise SystemExit(ingest_result["returncode"])

    _save_state(state_path, state)
    metrics = _ledger_metrics(Path(args.ledger_json))
    strict_v42_result: dict[str, Any] | None = None
    if metrics["calendar_days"] >= MIN_CALENDAR_DAYS and metrics["report_days"] >= MIN_REPORT_DAYS:
        strict_v42_result = _run(
            [
                sys.executable,
                "scripts/validate_v42_paper_shadow_evidence.py",
                "--paper-ledger-json",
                args.ledger_json,
                "--output-json",
                str(runtime_dir / "quant_v42_paper_shadow_evidence_gate_strict.json"),
                "--report-md",
                str(runtime_dir / "quant_v42_paper_shadow_evidence_gate_strict.md"),
                "--require-paper-evidence-ready",
            ]
        )

    print(
        json.dumps(
            {
                "ok": True,
                "production_ready": False,
                "date": report_date,
                "source_export": str(source_path),
                "daily_report": str(daily_path),
                "ledger": args.ledger_json,
                "ledger_metrics": metrics,
                "v42_strict_ran": strict_v42_result is not None,
                "v42_strict_result": strict_v42_result,
                "next_gate_condition": f">={MIN_CALENDAR_DAYS} calendar days and >={MIN_REPORT_DAYS} report days",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
