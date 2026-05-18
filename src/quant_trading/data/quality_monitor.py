"""
Data quality monitor — continuous multi-symbol quality surveillance with alerting.

AGENTS.md §4 (data-002): 数据质量监控与告警系统
- 缺失率 ≤1%, 价格跳动 ≤20%, 延迟 ≤30s
- 两个独立数据源交叉验证
- 告警收敛 (5分钟内相同事件只发一次)
- 数据质量看板 (Grafana) 显示各品种健康度
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

import pandas as pd

from .provider import Frequency
from .validator import DataValidator, ValidationResult

logger = logging.getLogger(__name__)
UTC = timezone.utc


# ── Alert ─────────────────────────────────────────────────────────────────────


@dataclass
class Alert:
    """A quality alert event."""
    symbol: str
    severity: str  # "error", "warning", "info"
    message: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    dedup_key: str = ""  # Used for alert convergence

    def __post_init__(self):
        if not self.dedup_key:
            self.dedup_key = f"{self.symbol}:{self.severity}:{hash(self.message) % 10000}"


class AlertManager:
    """
    Alert manager with 5-minute convergence.
    AGENTS.md §10: 告警收敛 — 相同事件 5 分钟内只发送一次。
    """

    CONVERGENCE_WINDOW_SECONDS = 300  # 5 minutes

    def __init__(self):
        self._alerts: list[Alert] = []
        self._last_sent: dict[str, float] = {}  # dedup_key → epoch time
        self._callbacks: list[Callable[[Alert], None]] = []

    def register_callback(self, callback: Callable[[Alert], None]) -> None:
        """Register an alert handler (e.g., Telegram, Email)."""
        self._callbacks.append(callback)

    def emit(self, alert: Alert) -> bool:
        """
        Emit an alert. Returns True if alert was sent, False if suppressed by convergence.
        """
        now = time.time()
        last = self._last_sent.get(alert.dedup_key, 0)
        if now - last < self.CONVERGENCE_WINDOW_SECONDS:
            logger.debug("Alert suppressed (convergence): %s", alert.dedup_key)
            return False

        self._last_sent[alert.dedup_key] = now
        self._alerts.append(alert)

        for cb in self._callbacks:
            try:
                cb(alert)
            except Exception:
                logger.exception("Alert callback failed for %s", alert.dedup_key)

        return True

    def get_recent_alerts(self, limit: int = 100) -> list[Alert]:
        """Get recent alerts."""
        return self._alerts[-limit:]


# ── Health score ──────────────────────────────────────────────────────────────


@dataclass
class SymbolHealth:
    """Per-symbol quality health score (0–100)."""
    symbol: str
    score: float = 100.0
    missing_ok: bool = True
    price_jumps_ok: bool = True
    freshness_ok: bool = True
    validation_result: Optional[ValidationResult] = None
    last_checked: Optional[datetime] = None

    @property
    def status(self) -> str:
        if self.score >= 80:
            return "healthy"
        if self.score >= 60:
            return "degraded"
        if self.score >= 40:
            return "warning"
        return "critical"


# ── Quality monitor ───────────────────────────────────────────────────────────


class DataQualityMonitor:
    """
    Continuous quality monitor. Runs DataValidator on multiple symbols,
    tracks health scores, cross-validates across independent sources,
    and emits alerts on quality degradation.

    AGENTS.md §4 (data-002):
      "DataQualityMonitor 类，检查缺失率、价格跳动、延迟"
      "两个独立数据源交叉验证"
    """

    def __init__(self):
        self.validator = DataValidator()
        self.alert_manager = AlertManager()
        self._health: dict[str, SymbolHealth] = {}

    # ── Public API ────────────────────────────────────────────────────────

    def check_single(
        self,
        symbol: str,
        df: pd.DataFrame,
        *,
        skip_freshness: bool = True,
    ) -> SymbolHealth:
        """Run quality checks on a single symbol's data."""
        result = self.validator.validate(symbol, df, skip_freshness=skip_freshness)
        health = self._compute_health(symbol, result)
        self._health[symbol] = health

        if not health.validation_result or not health.validation_result.passed:
            self.alert_manager.emit(Alert(
                symbol=symbol,
                severity="error" if health.score < 60 else "warning",
                message=f"Quality check failed: score={health.score:.0f}, "
                        f"errors={health.validation_result.errors if health.validation_result else 'N/A'}",
            ))
        elif health.score < 100:
            self.alert_manager.emit(Alert(
                symbol=symbol,
                severity="info",
                message=f"Quality warning: score={health.score:.0f}",
            ))

        logger.info(
            "Quality check: %s → score=%.0f status=%s",
            symbol, health.score, health.status,
        )
        return health

    def check_batch(
        self,
        data: dict[str, pd.DataFrame],
        *,
        skip_freshness: bool = True,
    ) -> dict[str, SymbolHealth]:
        """Run quality checks on multiple symbols."""
        results = {}
        for symbol, df in data.items():
            results[symbol] = self.check_single(symbol, df, skip_freshness=skip_freshness)
        return results

    def cross_validate_pair(
        self,
        symbol: str,
        df1: pd.DataFrame,
        df2: pd.DataFrame,
        source1: str,
        source2: str,
    ) -> ValidationResult:
        """
        Cross-validate the same symbol from two independent data sources.
        AGENTS.md §4: 差异 < 0.5%.
        """
        result = self.validator.cross_validate(df1, df2, source1, source2)

        if not result.passed:
            self.alert_manager.emit(Alert(
                symbol=symbol,
                severity="error",
                message=f"Cross-validation FAILED: {source1} vs {source2} "
                        f"max_diff={result.stats.get('max_diff_pct', 'N/A')}",
            ))

        return result

    def get_health(self, symbol: str) -> Optional[SymbolHealth]:
        """Get health status for a symbol."""
        return self._health.get(symbol)

    def get_all_health(self) -> dict[str, SymbolHealth]:
        """Get all tracked health statuses."""
        return dict(self._health)

    def get_dashboard_data(self) -> dict:
        """
        Get data suitable for Grafana dashboard display.
        Returns health scores, statuses, and recent alerts.
        """
        return {
            "timestamp": datetime.now(UTC).isoformat(),
            "symbols": [
                {
                    "symbol": s,
                    "score": h.score,
                    "status": h.status,
                    "missing_ok": h.missing_ok,
                    "price_jumps_ok": h.price_jumps_ok,
                    "freshness_ok": h.freshness_ok,
                    "last_checked": h.last_checked.isoformat() if h.last_checked else None,
                }
                for s, h in self._health.items()
            ],
            "recent_alerts": [
                {"symbol": a.symbol, "severity": a.severity, "message": a.message,
                 "timestamp": a.timestamp.isoformat()}
                for a in self.alert_manager.get_recent_alerts(20)
            ],
        }

    # ── Internal ──────────────────────────────────────────────────────────

    def _compute_health(
        self, symbol: str, result: ValidationResult
    ) -> SymbolHealth:
        """Compute 0-100 health score from validation result."""
        score = 100.0

        # Missing value penalty: -20 per column exceeding threshold
        for col in ["missing_open", "missing_high", "missing_low", "missing_close", "missing_volume"]:
            pct = float(result.stats.get(col, 0))
            if pct > self.validator.max_missing_pct:
                score -= 20

        # Price jumps: -15 if >1% of data has jumps
        jump_pct = float(result.stats.get("price_jump_pct", 0))
        if jump_pct > 0.01:
            score -= 15
        elif jump_pct > 0:
            score -= 5

        # Duplicates: -10
        dupes = int(result.stats.get("duplicates", 0))
        if dupes > 0:
            score -= 10

        # Data age: -10 per hour beyond threshold (only for real-time data)
        age = float(result.stats.get("data_age_seconds", 0))
        if age > self.validator.max_data_delay_seconds:
            extra_hours = (age - self.validator.max_data_delay_seconds) / 3600.0
            score -= min(30, int(extra_hours * 10))

        health = SymbolHealth(
            symbol=symbol,
            score=max(0.0, score),
            missing_ok=all(
                float(result.stats.get(f"missing_{c}", 0)) <= self.validator.max_missing_pct
                for c in ["open", "high", "low", "close", "volume"]
                if f"missing_{c}" in result.stats
            ),
            price_jumps_ok=jump_pct <= 0.01,
            freshness_ok=age <= self.validator.max_data_delay_seconds,
            validation_result=result,
            last_checked=datetime.now(UTC),
        )
        return health
