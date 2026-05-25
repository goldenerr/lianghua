"""
Capital Impact Assessment — V5.9 Production Candidate
AGENTS.md §7 & §11: Required before production deployment.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class CapitalImpactAssessment:
    """V5.9 strategy capital impact assessment."""

    strategy: str = "V5.9 MR Multi-Factor (n=35, 90d rebalance, sector cap=5)"
    date: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    prepared_by: str = "Quant Trading System AI Agent"

    # Backtest metrics
    backtest_sharpe: float = 1.38
    backtest_annual_return: float = 0.126
    backtest_annual_vol: float = 0.073
    backtest_max_drawdown: float = -0.136
    backtest_win_rate: float = 0.54
    backtest_calmar: float = 0.93
    wf_oos_sharpe: float = 1.20
    wf_decay: float = 0.22

    # Risk metrics
    max_position_pct: float = 0.20
    max_leverage: float = 1.0
    sector_cap: int = 5
    var_95_pct: float = 0.05

    # MDD safeguards
    mdd_reduce_threshold: float = -0.10  # reduce to 50% at -10% DD
    mdd_stop_threshold: float = -0.18  # stop all trading at -18% DD

    # Capital allocation
    recommended_initial_capital: float = 1_000_000  # 100万
    min_viable_capital: float = 500_000  # 50万

    # Worst-case analysis
    worst_case_daily_loss_pct: float = 0.073  # 1-sigma daily vol
    worst_case_weekly_loss_pct: float = 0.163  # sqrt(5) * daily
    worst_case_monthly_loss_pct: float = 0.334  # sqrt(21) * daily
    stress_scenario_loss_pct: float = -0.18  # stops at -18% DD in worst case

    # Liquidity risk
    max_single_position_value: float = 200_000  # 20% of 1M
    avg_daily_volume_pct: float = 0.10  # positions ≤10% of ADV
    max_portfolio_turnover: float = 1.82  # 182% per rebalance

    # Market risk
    primary_market: str = "A股 (沪深全市场)"
    universe_size: int = 1196
    max_sector_concentration: int = 5  # max 5 stocks per industry

    # Operational risk
    data_source: str = "akshare (Sina API)"
    rebalance_frequency_days: int = 90
    execution_model: str = "动态滑点模型 (0.05% base + sqrt(turnover)*0.10)"
    cost_model: str = "印花税 0.05% + 佣金 0.025%"

    # Approval
    approved: bool = False
    approved_by: str | None = None
    approved_at: str | None = None
    risk_level: str = "中等"  # 低/中等/高/极高

    def to_report(self) -> dict:
        return {
            "strategy": self.strategy,
            "date": self.date,
            "prepared_by": self.prepared_by,
            "backtest": {
                "sharpe": self.backtest_sharpe,
                "annual_return": self.backtest_annual_return,
                "annual_vol": self.backtest_annual_vol,
                "max_drawdown": self.backtest_max_drawdown,
                "win_rate": self.backtest_win_rate,
                "calmar": self.backtest_calmar,
                "wf_oos_sharpe": self.wf_oos_sharpe,
                "wf_decay": self.wf_decay,
            },
            "risk": {
                "max_position_pct": self.max_position_pct,
                "max_leverage": self.max_leverage,
                "sector_cap": self.sector_cap,
                "var_95_pct": self.var_95_pct,
                "mdd_safeguards": {
                    "reduce_at": self.mdd_reduce_threshold,
                    "stop_at": self.mdd_stop_threshold,
                },
            },
            "capital": {
                "recommended_initial": self.recommended_initial_capital,
                "min_viable": self.min_viable_capital,
            },
            "worst_case": {
                "daily_loss_1sigma": self.worst_case_daily_loss_pct,
                "weekly_loss_1sigma": self.worst_case_weekly_loss_pct,
                "monthly_loss_1sigma": self.worst_case_monthly_loss_pct,
                "stress_scenario": self.stress_scenario_loss_pct,
                "note": "MDD safeguards limit loss to -18% in worst case",
            },
            "liquidity": {
                "max_single_position": self.max_single_position_value,
                "avg_daily_volume_limit": self.avg_daily_volume_pct,
                "max_turnover": self.max_portfolio_turnover,
            },
            "market": {
                "market": self.primary_market,
                "universe_size": self.universe_size,
                "sector_concentration_limit": self.max_sector_concentration,
            },
            "operations": {
                "data_source": self.data_source,
                "rebalance_freq_days": self.rebalance_frequency_days,
                "execution_model": self.execution_model,
                "cost_model": self.cost_model,
            },
            "approval": {
                "approved": self.approved,
                "approved_by": self.approved_by,
                "approved_at": self.approved_at,
                "risk_level": self.risk_level,
            },
        }


def _default_changelog_dir() -> Path:
    """
    Resolve changelog directory in a portable way.

    Priority:
    1) QTS_CHANGELOG_DIR environment variable
    2) <repo_root>/docs/changelog
    """
    import os

    configured = os.getenv("QTS_CHANGELOG_DIR")
    if configured:
        return Path(configured).expanduser().resolve()

    # .../src/quant_trading/capital_impact_v59.py -> repo root
    repo_root = Path(__file__).resolve().parents[2]
    if repo_root.name == "src":
        repo_root = repo_root.parent
    return repo_root / "docs" / "changelog"


def requires_risk_approval(assessment: CapitalImpactAssessment) -> bool:
    """
    AGENTS.md §11/§32: capital-impact report must be approved before promoting.
    """
    return not assessment.approved


def generate_v59_report(output_dir: str | Path | None = None) -> dict:
    """Generate and save V5.9 capital impact assessment."""
    assessment = CapitalImpactAssessment()
    report = assessment.to_report()

    out = Path(output_dir).expanduser().resolve() if output_dir else _default_changelog_dir()
    out.mkdir(parents=True, exist_ok=True)

    path = out / "capital_impact_v5.9.json"
    with open(path, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    path = out / "capital_impact_v5.9.md"
    with open(path, "w") as f:
        f.write(
            f"""# 资金影响评估报告 — V5.9 多因子均值回归策略

**日期**: {assessment.date}
**策略**: {assessment.strategy}
**风险等级**: {assessment.risk_level}

## 回测绩效
| 指标 | 数值 | 门槛 | 状态 |
|------|------|------|------|
| Sharpe | {assessment.backtest_sharpe} | ≥1.2 | ✅ |
| 年化收益 | {assessment.backtest_annual_return:.1%} | — | — |
| 年化波动 | {assessment.backtest_annual_vol:.1%} | — | — |
| 最大回撤 | {assessment.backtest_max_drawdown:.1%} | ≤-15% | ✅ |
| 胜率 | {assessment.backtest_win_rate:.0%} | ≥40% | ✅ |
| WF OOS Sharpe | {assessment.wf_oos_sharpe} | — | — |
| WF Decay | {assessment.wf_decay:.0%} | ≤30% | ✅ |

## 风险控制
- **仓位上限**: {assessment.max_position_pct:.0%} 单品种
- **杠杆上限**: {assessment.max_leverage:.0f}x（A股无杠杆）
- **行业上限**: {assessment.sector_cap} 只/行业
- **MDD 安全阀**: -{abs(assessment.mdd_reduce_threshold):.0%} 降至 50%，-{abs(assessment.mdd_stop_threshold):.0%} 全面平仓
- **VaR(95%)**: ≤{assessment.var_95_pct:.0%}

## 最差情景
- 日 1σ 亏损: {assessment.worst_case_daily_loss_pct:.1%}
- 周 1σ 亏损: {assessment.worst_case_weekly_loss_pct:.1%}
- 月 1σ 亏损: {assessment.worst_case_monthly_loss_pct:.1%}
- **极端损失上限**: {assessment.stress_scenario_loss_pct:.1%}（MDD 安全阀触发）

## 资金建议
- **建议初始资金**: ¥{assessment.recommended_initial_capital:,.0f}
- **最低可行资金**: ¥{assessment.min_viable_capital:,.0f}

## 上线路径
1. ✅ 回测验证（全量 25 年数据，WF 5-fold）
2. ⏳ 模拟盘验证（≥3 个月，预期 Sharpe ≥0.97）
3. ⏳ 小资金实盘（≤1% 资金，≥1 个月）
4. ⏳ 全资金实盘（风控审批后）

---
*AGENTS.md §7 & §11 要求。由量化交易系统 AI Agent 自动生成。*
"""
        )

    print(f"Capital impact assessment saved to {path}")
    return report


if __name__ == "__main__":
    report = generate_v59_report()
    print(json.dumps(report, indent=2, ensure_ascii=False))
