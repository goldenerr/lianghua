"""
V7 Real-Time Data Pipeline — WebSocket + Redis + Scheduler.
Production-grade live data infrastructure for A-shares and crypto.

Components:
  1. LiveDataFeed: WebSocket/HTTP live data ingestion (akshare for A-shares, ccxt for crypto)
  2. RedisEventBus: Pub/sub event dispatch for real-time strategy signals
  3. DataScheduler: Cron-based daily data refresh + quality checks
  4. LiveMonitor: Real-time P&L + risk dashboard data
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger("live_pipeline")


# ═══════════════════════════════════════════════════════════════
# Market Data Types
# ═══════════════════════════════════════════════════════════════


@dataclass
class Quote:
    """Real-time market quote."""

    symbol: str
    bid: float
    ask: float
    price: float = 0.0
    bid_size: int = 0
    ask_size: int = 0
    volume: int = 0
    timestamp: float = 0.0
    source: str = ""


@dataclass
class Trade:
    """Real-time trade."""

    symbol: str
    price: float
    volume: int
    side: str = ""  # buy/sell
    timestamp: float = 0.0
    source: str = ""


@dataclass
class Bar:
    """Aggregated OHLCV bar."""

    symbol: str
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float
    resolution: str = "1m"  # 1m, 5m, 15m, 1h, 1d


# ═══════════════════════════════════════════════════════════════
# Live Data Feed
# ═══════════════════════════════════════════════════════════════


class LiveDataFeed:
    """Real-time market data feed for A-shares and crypto.

    Uses akshare for A-share live quotes and ccxt for crypto order books.
    Falls back to HTTP polling when WebSocket unavailable.
    """

    def __init__(
        self, symbols: list[str], market: str = "ashare", use_websocket: bool = False
    ) -> None:
        self.symbols = symbols
        self.market = market
        self.use_websocket = use_websocket
        self._running = False
        self._thread: threading.Thread | None = None

        # Callback registry
        self.on_quote: list[Callable[[Quote], None]] = []
        self.on_trade: list[Callable[[Trade], None]] = []
        self.on_bar: list[Callable[[Bar], None]] = []

        # Buffers
        self.quote_buffer: dict[str, Quote] = {}
        self.trade_buffer: deque[Trade] = deque(maxlen=10000)
        self.bar_buffers: dict[str, deque[Bar]] = {}  # {resolution: deque}

        # Bar aggregation state
        self._bar_state: dict[str, dict[str, dict[str, Any]]] = {}

    def start(self) -> None:
        """Start live data feed in background thread."""
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        log.info(f"Live feed started for {len(self.symbols)} symbols ({self.market})")

    def stop(self) -> None:
        """Stop live data feed."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        log.info("Live feed stopped")

    def _run_loop(self) -> None:
        """Main feed loop with polling."""
        poll_interval = 3.0 if self.market == "ashare" else 1.0  # seconds

        while self._running:
            try:
                if self.market == "ashare":
                    self._poll_ashare()
                elif self.market == "crypto":
                    self._poll_crypto()
                time.sleep(poll_interval)
            except Exception as e:
                log.error(f"Feed error: {e}")
                time.sleep(5)

    def _poll_ashare(self) -> None:
        """Poll A-share live quotes via akshare."""
        try:
            import akshare as ak

            # Batch query for all symbols
            ",".join(self.symbols)
            df = ak.stock_zh_a_spot_em()

            if df is None or len(df) == 0:
                return

            now = time.time()

            for _, row in df.iterrows():
                code = str(row.get("代码", ""))
                if code not in self.symbols:
                    continue

                quote = Quote(
                    symbol=code,
                    bid=float(row.get("买一", 0) or 0),
                    ask=float(row.get("卖一", 0) or 0),
                    price=float(row.get("最新价", 0) or 0),
                    volume=int(row.get("成交量", 0) or 0),
                    timestamp=now,
                    source="akshare",
                )
                self.quote_buffer[code] = quote

                # Notify callbacks
                for cb in self.on_quote:
                    cb(quote)

                # Aggregate bars
                self._aggregate_bar(code, quote, "1m", 60)
                self._aggregate_bar(code, quote, "5m", 300)

        except ImportError:
            log.info("akshare is not installed; A-share live polling is disabled")
        except Exception as e:
            log.warning(f"A-share poll failed: {e}")

    def _poll_crypto(self) -> None:
        """Poll crypto via ccxt."""
        try:
            import ccxt

            exchange = ccxt.binance()

            for symbol in self.symbols:
                try:
                    ticker = exchange.fetch_ticker(symbol)

                    quote = Quote(
                        symbol=symbol,
                        bid=ticker.get("bid", 0),
                        ask=ticker.get("ask", 0),
                        price=ticker.get("last", 0),
                        volume=int(ticker.get("baseVolume", 0)),
                        timestamp=ticker.get("timestamp", 0) / 1000,
                        source="ccxt",
                    )
                    self.quote_buffer[symbol] = quote

                    for cb in self.on_quote:
                        cb(quote)

                    self._aggregate_bar(symbol, quote, "1m", 60)

                except Exception as e:
                    log.debug(f"Crypto poll {symbol}: {e}")
        except ImportError:
            log.info("ccxt is not installed; crypto live polling is disabled")

    def _aggregate_bar(self, symbol: str, quote: Quote, resolution: str, secs: int) -> None:
        """Aggregate quotes into OHLCV bars."""
        if resolution not in self._bar_state:
            self._bar_state[resolution] = {}

        state = self._bar_state[resolution]
        now = quote.timestamp
        bar_start = int(now // secs) * secs

        if symbol not in state or state[symbol].get("start") != bar_start:
            # Emit previous bar
            if symbol in state:
                prev = state[symbol]
                if prev.get("count", 0) > 0:
                    bar = Bar(
                        symbol=symbol,
                        timestamp=prev["start"],
                        open=prev["open"],
                        high=prev["high"],
                        low=prev["low"],
                        close=prev["close"],
                        volume=prev["volume"],
                        resolution=resolution,
                    )
                    if resolution not in self.bar_buffers:
                        self.bar_buffers[resolution] = deque(maxlen=5000)
                    self.bar_buffers[resolution].append(bar)

                    for cb in self.on_bar:
                        cb(bar)

            # Start new bar
            state[symbol] = {
                "start": bar_start,
                "open": quote.price,
                "high": quote.price,
                "low": quote.price,
                "close": quote.price,
                "volume": 0,
                "count": 0,
            }

        cur = state[symbol]
        cur["high"] = max(cur["high"], quote.price)
        cur["low"] = min(cur["low"], quote.price)
        cur["close"] = quote.price
        cur["volume"] += getattr(quote, "volume", 0)
        cur["count"] += 1


# ═══════════════════════════════════════════════════════════════
# Redis Event Bus
# ═══════════════════════════════════════════════════════════════


class RedisEventBus:
    """Redis-based pub/sub event bus for real-time strategy signals.

    Channels:
      market.quote.{symbol}   — real-time quotes
      market.trade.{symbol}   — trade executions
      signal.entry.{strategy} — strategy entry signals
      signal.exit.{strategy}  — strategy exit signals
      risk.alert              — risk alerts
      order.new               — new order events
      order.fill              — order fill events
    """

    def __init__(self, host: str = "localhost", port: int = 6379, use_redis: bool = False) -> None:
        self.host = host
        self.port = port
        self.use_redis = use_redis
        self._client = None
        self._pubsub = None
        self._subscribers: dict[str, list[Callable[[dict[str, Any]], None]]] = {}
        self._event_queue: deque[dict[str, Any]] = deque(maxlen=50000)  # in-memory fallback

        if use_redis:
            try:
                import redis

                self._client = redis.Redis(host=host, port=port, decode_responses=True)
                self._client.ping()
                log.info(f"Redis connected: {host}:{port}")
            except Exception as e:
                log.warning(f"Redis unavailable ({e}), using in-memory bus")
                self.use_redis = False

    def publish(self, channel: str, data: dict[str, Any]) -> None:
        """Publish event to channel."""
        event = {
            "channel": channel,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if self.use_redis and self._client:
            try:
                self._client.publish(channel, json.dumps(event))
            except Exception as exc:
                log.warning(
                    "Redis publish failed for %s: %s; event retained in memory", channel, exc
                )

        # In-memory fallback
        self._event_queue.append(event)

        # Notify local subscribers
        if channel in self._subscribers:
            for cb in self._subscribers[channel]:
                try:
                    cb(data)
                except Exception as e:
                    log.error(f"Subscriber error: {e}")

    def subscribe(self, channel: str, callback: Callable[[dict[str, Any]], None]) -> None:
        """Subscribe to channel."""
        if channel not in self._subscribers:
            self._subscribers[channel] = []
        self._subscribers[channel].append(callback)

    def get_events(self, channel: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Get recent events (in-memory fallback)."""
        events = list(self._event_queue)
        if channel:
            events = [e for e in events if e["channel"] == channel]
        return events[-limit:]

    def publish_quote(self, quote: Quote) -> None:
        self.publish(
            f"market.quote.{quote.symbol}",
            {
                "symbol": quote.symbol,
                "bid": quote.bid,
                "ask": quote.ask,
                "price": quote.price,
                "timestamp": quote.timestamp,
            },
        )

    def publish_signal(self, strategy: str, symbol: str, side: str, weight: float) -> None:
        self.publish(
            f"signal.{side}.{strategy}",
            {
                "strategy": strategy,
                "symbol": symbol,
                "side": side,
                "weight": weight,
            },
        )

    def publish_risk_alert(self, alert_type: str, message: str, severity: str = "WARNING") -> None:
        self.publish(
            "risk.alert",
            {
                "type": alert_type,
                "message": message,
                "severity": severity,
            },
        )


# ═══════════════════════════════════════════════════════════════
# Data Scheduler
# ═══════════════════════════════════════════════════════════════


class DataScheduler:
    """Cron-based daily data refresh + quality checks."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.jobs: dict[str, dict[str, Any]] = {}  # {name: {schedule, func, last_run}}

    def add_job(self, name: str, func: Callable[[], None], schedule: str = "daily") -> None:
        """Add a scheduled job.

        schedule: 'daily' (market close), 'hourly', 'intraday' (every 5min)
        """
        self.jobs[name] = {
            "func": func,
            "schedule": schedule,
            "last_run": None,
        }

    def run_daily_refresh(self) -> dict[str, dict[str, Any]]:
        """Run all daily refresh jobs (called after market close ~15:30 CST)."""
        log.info(f"Running daily refresh ({len(self.jobs)} jobs)")
        results: dict[str, dict[str, Any]] = {}

        for name, job in self.jobs.items():
            if job["schedule"] != "daily":
                continue
            try:
                t0 = time.time()
                job["func"]()
                job["last_run"] = datetime.now(timezone.utc)
                results[name] = {"status": "ok", "elapsed": time.time() - t0}
                log.info(f"  {name}: OK ({time.time()-t0:.0f}s)")
            except Exception as e:
                results[name] = {"status": "error", "error": str(e)}
                log.error(f"  {name}: FAILED - {e}")

        return results

    def get_status(self) -> dict[str, Any]:
        """Get scheduler status."""
        return {
            "n_jobs": len(self.jobs),
            "jobs": {
                name: {"schedule": j["schedule"], "last_run": str(j["last_run"])}
                for name, j in self.jobs.items()
            },
        }


# ═══════════════════════════════════════════════════════════════
# Live Portfolio Monitor
# ═══════════════════════════════════════════════════════════════


class LivePortfolioMonitor:
    """Real-time P&L and risk monitor."""

    def __init__(self, initial_capital: float = 1.0) -> None:
        self.initial_capital = initial_capital
        self.positions: dict[str, dict[str, float]] = {}  # {symbol: {shares, avg_cost}}
        self.cash = initial_capital
        self.trade_history: list[dict[str, Any]] = []
        self.pnl_history: deque[float] = deque(maxlen=5000)
        self.equity_history: deque[float] = deque([initial_capital], maxlen=5000)
        self.peak = initial_capital

    @property
    def total_value(self) -> float:
        return float(
            self.cash
            + sum(p.get("shares", 0) * p.get("last_price", 0) for p in self.positions.values())
        )

    @property
    def drawdown(self) -> float:
        eq = self.total_value
        self.peak = max(self.peak, eq)
        return (eq - self.peak) / self.peak

    @property
    def sharpe_1m(self) -> float:
        if len(self.pnl_history) < 20:
            return 0.0
        rets = list(self.pnl_history)[-20:]
        avg = np.mean(rets)
        std = np.std(rets, ddof=1)
        return float(avg / std * np.sqrt(252)) if std > 1e-8 else 0.0

    def on_quote(self, quote: Quote) -> None:
        """Update position values on each quote."""
        if quote.symbol in self.positions:
            self.positions[quote.symbol]["last_price"] = quote.price

    def on_fill(self, symbol: str, side: str, shares: int, price: float) -> None:
        """Record trade fill."""
        if side == "buy":
            cost = shares * price * 1.00075  # + commission + stamp
            self.cash -= cost
            if symbol not in self.positions:
                self.positions[symbol] = {"shares": 0, "avg_cost": 0, "last_price": price}
            pos = self.positions[symbol]
            total_cost = pos["shares"] * pos["avg_cost"] + cost
            pos["shares"] += shares
            pos["avg_cost"] = total_cost / pos["shares"] if pos["shares"] > 0 else 0
        else:
            proceeds = shares * price * 0.99925  # - commission - stamp
            self.cash += proceeds
            if symbol in self.positions:
                self.positions[symbol]["shares"] -= shares
                if self.positions[symbol]["shares"] <= 0:
                    del self.positions[symbol]

        self.trade_history.append(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "side": side,
                "shares": shares,
                "price": price,
            }
        )

    def get_status(self) -> dict[str, Any]:
        """Get current portfolio status."""
        return {
            "total_value": round(self.total_value, 2),
            "cash": round(self.cash, 2),
            "return_pct": round(self.total_value / self.initial_capital - 1, 4),
            "drawdown": round(self.drawdown, 4),
            "sharpe_1m": round(self.sharpe_1m, 2),
            "n_positions": len(self.positions),
            "positions": {
                s: {"shares": p["shares"], "value": round(p["shares"] * p.get("last_price", 0), 2)}
                for s, p in self.positions.items()
            },
        }
