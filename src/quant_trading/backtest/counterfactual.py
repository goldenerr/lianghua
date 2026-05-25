"""
Counterfactual backtesting & stress test engine (backtest-003).

AGENTS.md §5 (backtest-003):
  - 反事实模拟：修改历史市场路径评估策略鲁棒性
  - 压力测试场景：2020.3, 2015.8, 2022 熊市 + 自定义
  - 生成压力测试报告（最大回撤、最大杠杆、流动性缺口）
  - 参数扰动测试（修改滑点、延迟等）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

import numpy as np

UTC = timezone.utc


# ── Scenarios ─────────────────────────────────────────────────────────────────


class ScenarioType(str, Enum):
    REMOVE_OUTLIERS = "remove_outliers"  # Drop extreme returns
    SHOCK_PERIOD = "shock_period"  # Apply shock to date range
    BOOTSTRAP = "bootstrap"  # Resample returns
    VOLATILITY_SPIKE = "volatility_spike"  # Amplify volatility
    LIQUIDITY_CRUNCH = "liquidity_crunch"  # Reduce volume + widen spreads
    HISTORICAL = "historical"  # Pre-built historical scenario
    CUSTOM = "custom"  # User-defined


@dataclass
class CounterfactualScenario:
    """A single alternative market path scenario."""

    name: str
    scenario_type: ScenarioType
    # Shock period
    shock_value: float = 0.0  # Daily shock (e.g., -0.03 for -3%/day)
    shock_start_idx: int = 0
    shock_duration: int = 20  # Days
    # Volatility spike
    vol_multiplier: float = 3.0  # Multiply returns by this
    # Outlier removal
    outlier_threshold: float = 4.0  # Z-score threshold
    # Liquidity crunch
    volume_reduction: float = 0.5  # Reduce volume to 50%
    spread_widening: float = 3.0  # Widen spreads to 3x
    # Metadata
    description: str = ""

    def apply(self, returns: np.ndarray, volumes: np.ndarray | None = None) -> np.ndarray:
        """
        Apply scenario to a return series. Returns modified returns.
        Volume modifications are applied in-place if volumes provided.
        """
        r = returns.copy().astype(np.float64)

        if self.scenario_type == ScenarioType.SHOCK_PERIOD:
            end = min(self.shock_start_idx + self.shock_duration, len(r))
            r[self.shock_start_idx : end] += self.shock_value

        elif self.scenario_type == ScenarioType.VOLATILITY_SPIKE:
            r = r * self.vol_multiplier

        elif self.scenario_type == ScenarioType.REMOVE_OUTLIERS:
            z = (r - r.mean()) / (r.std() + 1e-10)
            r[np.abs(z) > self.outlier_threshold] = 0.0

        elif self.scenario_type == ScenarioType.LIQUIDITY_CRUNCH:
            if volumes is not None:
                volumes[:] = volumes * self.volume_reduction

        elif self.scenario_type == ScenarioType.BOOTSTRAP:
            n = len(r)
            rng = np.random.RandomState(42)
            r = rng.choice(r, size=n, replace=True)

        return r


# ── Historical stress scenarios ───────────────────────────────────────────────


class HistoricalScenarios:
    """
    Pre-built historical crisis scenarios.

    Returns daily return shocks for each scenario period.
    AGENTS.md §5: 2020.3 熔断、2015.8 股灾、2022 熊市
    """

    @staticmethod
    def covid_crash_2020() -> CounterfactualScenario:
        """
        COVID-19 crash (Feb-Mar 2020).
        S&P 500 dropped ~34% in 23 trading days. Multiple circuit breakers.
        """
        return CounterfactualScenario(
            name="COVID-19 Crash 2020.03",
            scenario_type=ScenarioType.HISTORICAL,
            shock_value=-0.05,  # -5% daily
            shock_start_idx=0,
            shock_duration=23,  # 23 trading days
            vol_multiplier=3.0,  # Extreme volatility
            description="S&P 500 -34% in 23 days. 4 circuit breakers triggered. "
            "VIX spiked to 82.69.",
        )

    @staticmethod
    def china_crash_2015() -> CounterfactualScenario:
        """
        China A-share crash (Jun-Aug 2015).
        Shanghai Composite dropped ~43% from peak.
        Thousands of stocks hit limit-down daily.
        """
        return CounterfactualScenario(
            name="China A-Share Crash 2015.08",
            scenario_type=ScenarioType.HISTORICAL,
            shock_value=-0.06,  # -6% daily (limit down)
            shock_start_idx=0,
            shock_duration=15,
            vol_multiplier=2.5,
            description="Shanghai Composite -43%. Mass limit-downs. "
            "Government intervention with 'national team' buying.",
        )

    @staticmethod
    def bear_market_2022() -> CounterfactualScenario:
        """
        2022 bear market (Jan-Oct 2022).
        Fed rate hikes, S&P 500 -25%, NASDAQ -33%.
        Bond + equity correlation broke. Crypto winter.
        """
        return CounterfactualScenario(
            name="Bear Market 2022",
            scenario_type=ScenarioType.HISTORICAL,
            shock_value=-0.015,  # -1.5% daily (sustained grind)
            shock_start_idx=0,
            shock_duration=60,  # ~3 months of grinding
            vol_multiplier=1.5,
            description="Fed rate hikes, S&P -25%, NASDAQ -33%. "
            "Bond-equity correlation broke. Crypto -65%.",
        )

    @staticmethod
    def flash_crash() -> CounterfactualScenario:
        """Generic flash crash scenario — sudden liquidity evaporation."""
        return CounterfactualScenario(
            name="Flash Crash",
            scenario_type=ScenarioType.LIQUIDITY_CRUNCH,
            volume_reduction=0.1,  # Volume drops to 10%
            spread_widening=10.0,  # Spreads 10x wider
            shock_value=-0.08,
            shock_duration=3,
            description="Sudden liquidity evaporation. Volume -90%, spreads 10x. "
            "Algorithmic cascades. Recovers within hours.",
        )

    @staticmethod
    def stagflation() -> CounterfactualScenario:
        """1970s-style stagflation: low growth + high inflation."""
        return CounterfactualScenario(
            name="Stagflation",
            scenario_type=ScenarioType.SHOCK_PERIOD,
            shock_value=-0.003,  # -0.3% daily grind
            shock_start_idx=0,
            shock_duration=120,  # ~6 months
            vol_multiplier=2.0,
            description="Low growth + high inflation. Bonds and equities both suffer. "
            "Commodities outperform.",
        )

    @classmethod
    def all_scenarios(cls) -> list[CounterfactualScenario]:
        """Return all pre-built historical scenarios."""
        return [
            cls.covid_crash_2020(),
            cls.china_crash_2015(),
            cls.bear_market_2022(),
            cls.flash_crash(),
            cls.stagflation(),
        ]


# ── Stress test engine ────────────────────────────────────────────────────────


@dataclass
class StressTestResult:
    """Result of a single stress test run."""

    scenario_name: str
    # Returns
    total_return: float
    annualized_return: float
    annualized_volatility: float
    sharpe_ratio: float
    # Risk
    max_drawdown: float
    max_drawdown_days: int
    max_leverage: float
    var_95: float
    cvar_95: float
    # Liquidity
    liquidity_gap_pct: float  # % of days where volume < threshold
    worst_day_return: float
    # Comparison
    vs_baseline_return: float  # Difference from baseline total return
    passed: bool = True  # Whether risk limits not breached


@dataclass
class StressTestReport:
    """Aggregate stress test report across multiple scenarios."""

    baseline: StressTestResult
    scenarios: list[StressTestResult] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def worst_case(self) -> StressTestResult | None:
        if not self.scenarios:
            return None
        return min(self.scenarios, key=lambda r: r.total_return)

    @property
    def max_drawdown_across_all(self) -> float:
        if not self.scenarios:
            return self.baseline.max_drawdown
        # max_drawdown is negative — worst is the most negative (min)
        return min(r.max_drawdown for r in [*self.scenarios, self.baseline])

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.scenarios)

    def summary(self) -> dict:
        """Generate summary for reporting."""
        base = self.baseline
        return {
            "baseline_return": round(base.total_return, 4),
            "baseline_sharpe": round(base.sharpe_ratio, 2),
            "baseline_max_dd": round(base.max_drawdown, 4),
            "scenarios_tested": len(self.scenarios),
            "all_passed": self.all_passed,
            "worst_return": round(self.worst_case.total_return, 4) if self.worst_case else None,
            "worst_max_dd": round(self.max_drawdown_across_all, 4),
            "vs_baseline_worst": (
                round(self.worst_case.vs_baseline_return, 4) if self.worst_case else None
            ),
        }


class StressTestEngine:
    """
    Run stress tests against a strategy's return series.

    AGENTS.md §5 (backtest-003):
      "整合现有压力测试场景（2020.3, 2015.8, 2022 熊市等场景库）"
      "生成压力测试报告（最大回撤、最大杠杆、流动性缺口）"
    """

    def __init__(
        self,
        max_drawdown_limit: float = 0.25,
        var_95_limit: float = 0.05,
        liquidity_threshold_pct: float = 0.10,
    ):
        self.max_drawdown_limit = max_drawdown_limit
        self.var_95_limit = var_95_limit
        self.liquidity_threshold_pct = liquidity_threshold_pct

    def compute_baseline(
        self,
        returns: np.ndarray,
        volumes: np.ndarray | None = None,
    ) -> StressTestResult:
        """Compute baseline metrics from the actual return series."""
        return self._compute_metrics("Baseline", returns, volumes, None)

    def run_scenario(
        self,
        returns: np.ndarray,
        scenario: CounterfactualScenario,
        volumes: np.ndarray | None = None,
    ) -> StressTestResult:
        """Run a single counterfactual scenario."""
        modified_volumes = volumes.copy() if volumes is not None else None
        modified_returns = scenario.apply(returns, modified_volumes)
        return self._compute_metrics(
            scenario.name,
            modified_returns,
            modified_volumes,
            returns,
        )

    def run_all(
        self,
        returns: np.ndarray,
        scenarios: list[CounterfactualScenario] | None = None,
        volumes: np.ndarray | None = None,
    ) -> StressTestReport:
        """Run all scenarios and generate report."""
        if scenarios is None:
            scenarios = HistoricalScenarios.all_scenarios()

        baseline = self.compute_baseline(returns, volumes)
        results = [self.run_scenario(returns, s, volumes) for s in scenarios]
        return StressTestReport(baseline=baseline, scenarios=results)

    def _compute_metrics(
        self,
        name: str,
        returns: np.ndarray,
        volumes: np.ndarray | None,
        baseline_returns: np.ndarray | None,
    ) -> StressTestResult:
        """Compute all risk/return metrics from a return series."""
        r = np.asarray(returns, dtype=np.float64)

        # Cumulative return
        cumulative = np.cumprod(1 + r) - 1
        total_return = float(cumulative[-1]) if len(cumulative) > 0 else 0.0

        # Annualized metrics (252 trading days)
        ann_return = float(np.mean(r) * 252)
        ann_vol = float(np.std(r, ddof=1) * np.sqrt(252))
        sharpe = ann_return / ann_vol if ann_vol > 1e-10 else 0.0

        # Drawdown
        peak = np.maximum.accumulate(np.cumprod(1 + r))
        drawdown = (np.cumprod(1 + r) - peak) / peak
        max_dd = float(np.min(drawdown))
        max_dd_days = int(np.argmin(drawdown)) if len(drawdown) > 0 else 0

        # VaR / CVaR
        var_95 = float(np.percentile(r, 5))
        tail = r[r <= var_95]
        cvar_95 = float(np.mean(tail)) if len(tail) > 0 else var_95

        # Liquidity
        liquidity_gap = 0.0
        if volumes is not None and len(volumes) > 0:
            vol_threshold = np.median(volumes) * self.liquidity_threshold_pct
            liquidity_gap = float(np.mean(volumes < vol_threshold))

        # Leverage (simplified: assume max_leverage derived from VaR)
        max_leverage = abs(1.0 / var_95) if abs(var_95) > 1e-10 else 10.0

        # Worst day
        worst_day = float(np.min(r))

        # Baseline comparison
        vs_baseline = 0.0
        if baseline_returns is not None:
            base_total = float(np.cumprod(1 + np.asarray(baseline_returns))[-1] - 1)
            vs_baseline = total_return - base_total

        # Pass/fail
        passed = True
        if abs(max_dd) > self.max_drawdown_limit:
            passed = False
        if abs(var_95) > self.var_95_limit:
            passed = False

        return StressTestResult(
            scenario_name=name,
            total_return=round(total_return, 6),
            annualized_return=round(ann_return, 6),
            annualized_volatility=round(ann_vol, 6),
            sharpe_ratio=round(sharpe, 4),
            max_drawdown=round(max_dd, 6),
            max_drawdown_days=max_dd_days,
            max_leverage=round(max_leverage, 2),
            var_95=round(var_95, 6),
            cvar_95=round(cvar_95, 6),
            liquidity_gap_pct=round(liquidity_gap, 4),
            worst_day_return=round(worst_day, 6),
            vs_baseline_return=round(vs_baseline, 6),
            passed=passed,
        )


# ── Parameter perturbation ────────────────────────────────────────────────────


@dataclass
class PerturbationResult:
    """Result of a parameter perturbation test."""

    parameter: str
    base_value: float
    test_values: list[float]
    sharpe_values: list[float]
    return_values: list[float]
    max_dd_values: list[float]
    sensitivity: float = 0.0  # max_change / param_change


class ParameterPerturbation:
    """
    Test strategy sensitivity to parameter changes.

    AGENTS.md §5 (backtest-003):
      支持参数扰动测试（修改滑点、延迟等）
    """

    @staticmethod
    def test_slippage(
        base_returns: np.ndarray,
        slippage_bps_values: list[float] | None = None,
    ) -> PerturbationResult:
        """
        Test sensitivity to slippage (basis points).

        Each +1bp slippage = -0.0001 on daily returns.
        """
        if slippage_bps_values is None:
            slippage_bps_values = [0, 2, 5, 10, 20, 50]

        sharpes, rets, dds = [], [], []
        for bps in slippage_bps_values:
            adjusted = base_returns - (bps / 10000.0)
            sharpe = float(np.mean(adjusted) / (np.std(adjusted, ddof=1) + 1e-10) * np.sqrt(252))
            total_ret = float(np.cumprod(1 + adjusted)[-1] - 1)
            peak = np.maximum.accumulate(np.cumprod(1 + adjusted))
            dd = float(np.min((np.cumprod(1 + adjusted) - peak) / peak))
            sharpes.append(round(sharpe, 4))
            rets.append(round(total_ret, 4))
            dds.append(round(dd, 4))

        # Sensitivity = avg sharpe change per unit parameter change
        sensitivity = (
            abs(sharpes[-1] - sharpes[0]) / abs(slippage_bps_values[-1] - slippage_bps_values[0])
            if len(slippage_bps_values) > 1
            else 0.0
        )

        return PerturbationResult(
            parameter="slippage_bps",
            base_value=0.0,
            test_values=slippage_bps_values,
            sharpe_values=sharpes,
            return_values=rets,
            max_dd_values=dds,
            sensitivity=round(sensitivity, 6),
        )

    @staticmethod
    def test_costs(
        base_returns: np.ndarray,
        commission_bps_values: list[float] | None = None,
    ) -> PerturbationResult:
        """Test sensitivity to commission costs."""
        if commission_bps_values is None:
            commission_bps_values = [0, 1, 3, 5, 10, 15]

        return ParameterPerturbation.test_slippage(
            base_returns,
            commission_bps_values,
        )

    @staticmethod
    def test_latency(
        base_returns: np.ndarray,
        latency_ms_values: list[float] | None = None,
    ) -> PerturbationResult:
        """
        Test sensitivity to execution latency.

        Higher latency → worse fills → lower returns. Simplified: each +10ms = -0.5bp.
        """
        if latency_ms_values is None:
            latency_ms_values = [0, 10, 50, 100, 500, 1000]

        sharpes, rets, dds = [], [], []
        for ms in latency_ms_values:
            bps_impact = ms / 10.0 * 0.5  # 10ms → 0.5bp
            adjusted = base_returns - (bps_impact / 10000.0)
            sharpe = float(np.mean(adjusted) / (np.std(adjusted, ddof=1) + 1e-10) * np.sqrt(252))
            total_ret = float(np.cumprod(1 + adjusted)[-1] - 1)
            peak = np.maximum.accumulate(np.cumprod(1 + adjusted))
            dd = float(np.min((np.cumprod(1 + adjusted) - peak) / peak))
            sharpes.append(round(sharpe, 4))
            rets.append(round(total_ret, 4))
            dds.append(round(dd, 4))

        sensitivity = (
            abs(sharpes[-1] - sharpes[0]) / abs(latency_ms_values[-1] - latency_ms_values[0])
            if len(latency_ms_values) > 1
            else 0.0
        )

        return PerturbationResult(
            parameter="latency_ms",
            base_value=0.0,
            test_values=latency_ms_values,
            sharpe_values=sharpes,
            return_values=rets,
            max_dd_values=dds,
            sensitivity=round(sensitivity, 6),
        )
