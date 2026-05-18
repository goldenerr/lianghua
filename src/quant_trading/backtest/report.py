"""
Backtest report generator — standardized performance reports.

AGENTS.md §5: 生成标准化绩效报告：累计收益曲线、年化收益、夏普比率、最大回撤、卡玛比率、胜率、盈亏比
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from .engine import BacktestResult, PerformanceMetrics
from .monte_carlo import MonteCarloSimulator, StressTestRunner

UTC = timezone.utc


class ReportGenerator:
    """Generate standardized backtest reports."""

    @staticmethod
    def generate(
        result: BacktestResult,
        monte_carlo: Optional[dict] = None,
        stress_test: Optional[dict[str, dict]] = None,
        include_trades: bool = False,
    ) -> dict:
        """
        Generate a comprehensive backtest report.

        AGENTS.md §5: 标准化绩效报告 includes all required metrics.
        """
        m = result.metrics

        report = {
            "report_metadata": {
                "generated_at": datetime.now(UTC).isoformat(),
                "config": result.config.__dict__ if result.config else {},
                "quality_gates_passed": result.passed,
            },
            "performance_summary": m.to_dict(),
            "quality_gates": {
                "sharpe_passed": m.sharpe_ratio >= (result.config.min_sharpe if result.config else 1.2),
                "mdd_passed": abs(m.max_drawdown) <= (result.config.max_mdd if result.config else 0.15),
                "win_rate_passed": m.win_rate >= (result.config.min_win_rate if result.config else 0.40),
            },
            "warnings": result.warnings,
            "errors": result.errors,
        }

        # Walk-Forward section
        if result.wf_folds:
            report["walk_forward"] = {
                "n_folds": len(result.wf_folds),
                "oos_sharpe": round(result.wf_oos_metrics.sharpe_ratio, 4) if result.wf_oos_metrics else None,
                "folds": [f.to_dict() for f in result.wf_folds],
            }

        # Monte Carlo section
        if monte_carlo:
            report["monte_carlo"] = monte_carlo

        # Stress test section
        if stress_test:
            report["stress_test"] = stress_test

        # Trades
        if include_trades and not result.trades.empty:
            report["trades"] = result.trades.to_dict(orient="records")[:100]  # Limit to 100
            report["total_trades"] = len(result.trades)

        return report

    @staticmethod
    def to_json(report: dict, indent: int = 2) -> str:
        return json.dumps(report, indent=indent, ensure_ascii=False, default=str)

    @staticmethod
    def summary(report: dict) -> str:
        """Generate a human-readable summary string."""
        ps = report["performance_summary"]
        lines = [
            "╔══════════════════════════════════════════╗",
            "║        BACKTEST PERFORMANCE REPORT        ║",
            "╠══════════════════════════════════════════╣",
            f"║ Total Return:    {ps['total_return_pct']:>8.2f}%                ║",
            f"║ Annual Return:   {ps['annualized_return_pct']:>8.2f}%                ║",
            f"║ Sharpe Ratio:    {ps['sharpe_ratio']:>8.2f}                    ║",
            f"║ Max Drawdown:    {ps['max_drawdown_pct']:>8.2f}%                ║",
            f"║ Calmar Ratio:    {ps['calmar_ratio']:>8.2f}                    ║",
            f"║ Win Rate:        {ps['win_rate_pct']:>8.2f}%                ║",
            f"║ Profit Factor:   {ps['profit_factor']:>8.2f}                    ║",
            f"║ Total Trades:    {ps['total_trades']:>8}                      ║",
            f"║ VaR 95%:         {ps['var_95_pct']:>8.2f}%                ║",
            "╠══════════════════════════════════════════╣",
        ]

        qg = report["quality_gates"]
        lines.append(f"║ Quality Gates:                            ║")
        for gate, passed in qg.items():
            status = "✅" if passed else "❌"
            lines.append(f"║   {gate}: {status}                              ║")

        lines.append("╚══════════════════════════════════════════╝")
        return "\n".join(lines)
