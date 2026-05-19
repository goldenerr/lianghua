"""
Strategy parameter change impact assessment (strategy-003).

AGENTS.md §5 (strategy-003):
  - 修改策略参数前，自动运行短期 Walk-Forward 回测（最近3个月）
  - 输出变更前后 Sharpe、MDD、胜率等关键指标的变化百分比
  - 生成变更风险报告（包含建议：是否允许上线）
  - 报告需风控人员确认后方可生效
  - 评估结果记录审计日志
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np

UTC = timezone.utc


@dataclass
class ParameterChange:
    param_name: str
    old_value: float
    new_value: float
    change_pct: float = 0.0

    def __post_init__(self):
        if self.old_value != 0:
            self.change_pct = round((self.new_value - self.old_value) / abs(self.old_value) * 100, 2)


@dataclass
class ImpactMetrics:
    sharpe_before: float
    sharpe_after: float
    mdd_before: float
    mdd_after: float
    win_rate_before: float
    win_rate_after: float

    @property
    def sharpe_change_pct(self) -> float:
        if self.sharpe_before == 0:
            return 0.0
        return round((self.sharpe_after - self.sharpe_before) / abs(self.sharpe_before) * 100, 2)

    @property
    def mdd_change_pct(self) -> float:
        if self.mdd_before == 0:
            return 0.0
        return round((self.mdd_after - self.mdd_before) / abs(self.mdd_before) * 100, 2)


@dataclass
class RiskAssessment:
    risk_level: str  # "low", "medium", "high", "critical"
    approved: bool = False
    recommendations: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


@dataclass
class ChangeReport:
    strategy_name: str
    changes: list[ParameterChange]
    metrics: ImpactMetrics
    risk: RiskAssessment
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    auditor: str = "system"


def evaluate_impact(
    returns_before: np.ndarray,
    returns_after: np.ndarray,
    changes: list[ParameterChange],
    strategy_name: str = "",
) -> ChangeReport:
    """Evaluate the impact of parameter changes."""
    metrics = _compute_impact_metrics(returns_before, returns_after)
    risk = _assess_risk(metrics, changes)
    return ChangeReport(
        strategy_name=strategy_name,
        changes=changes,
        metrics=metrics,
        risk=risk,
    )


def _compute_impact_metrics(before: np.ndarray, after: np.ndarray, risk_free_rate: float = 0.025) -> ImpactMetrics:
    def _calc(r: np.ndarray) -> tuple:
        if len(r) < 2:
            return 0.0, 0.0, 0.0
        ann_ret = float(np.mean(r) * 252)
        ann_vol = float(np.std(r, ddof=1) * np.sqrt(252))
        if ann_vol > 1e-8:
            sharpe = (ann_ret - risk_free_rate) / ann_vol
            if abs(sharpe) > 100:
                sharpe = 0.0
        else:
            sharpe = 0.0
        peak = np.maximum.accumulate(np.cumprod(1 + r))
        mdd = float(np.min((np.cumprod(1 + r) - peak) / peak))
        wr = float(np.mean(r > 0))
        return sharpe, mdd, wr

    sb, mb, wb = _calc(before)
    sa, ma, wa = _calc(after)
    return ImpactMetrics(
        sharpe_before=round(sb, 4), sharpe_after=round(sa, 4),
        mdd_before=round(mb, 4), mdd_after=round(ma, 4),
        win_rate_before=round(wb, 4), win_rate_after=round(wa, 4),
    )


def _assess_risk(metrics: ImpactMetrics, changes: list[ParameterChange]) -> RiskAssessment:
    recommendations = []
    flags = []
    risk_score = 0

    # Sharpe degradation
    if metrics.sharpe_change_pct < -30:
        risk_score += 3
        flags.append(f"Sharpe degraded {metrics.sharpe_change_pct:.1f}%")
        recommendations.append("Reject — Sharpe ratio too degraded")
    elif metrics.sharpe_change_pct < -10:
        risk_score += 1
        flags.append(f"Sharpe declined {metrics.sharpe_change_pct:.1f}%")
        recommendations.append("Require manual review — Sharpe declined")

    # MDD increase
    if metrics.mdd_change_pct < -20:  # More negative = worse
        risk_score += 3
        flags.append(f"MDD worsened {abs(metrics.mdd_change_pct):.1f}%")
        recommendations.append("Reject — drawdown significantly worse")
    elif metrics.mdd_change_pct < -5:
        risk_score += 1

    # Large parameter changes
    for c in changes:
        if abs(c.change_pct) > 50:
            risk_score += 2
            flags.append(f"{c.param_name} changed {c.change_pct:.1f}% (>50%)")
            recommendations.append(f"Large change to {c.param_name} — verify with WF optimization")

    if risk_score == 0:
        level = "low"
    elif risk_score <= 2:
        level = "medium"
    elif risk_score <= 4:
        level = "high"
    else:
        level = "critical"

    return RiskAssessment(
        risk_level=level,
        approved=level == "low",
        recommendations=recommendations if recommendations else ["OK to proceed"],
        flags=flags,
    )
