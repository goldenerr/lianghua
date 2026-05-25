from datetime import date

import numpy as np
import pytest
from quant_trading.portfolio.attribution import AttributionReport, calculate_brinson
from quant_trading.portfolio.benchmark import BenchmarkRegistry, brinson_attribution


def test_brinson_effects_reconcile_to_direct_excess_return() -> None:
    portfolio_weights = [0.6, 0.4]
    benchmark_weights = [0.5, 0.5]
    portfolio_returns = [0.12, 0.03]
    benchmark_returns = [0.08, 0.04]

    result = calculate_brinson(
        portfolio_weights,
        benchmark_weights,
        portfolio_returns,
        benchmark_returns,
    )

    direct_excess = np.dot(portfolio_weights, portfolio_returns) - np.dot(
        benchmark_weights, benchmark_returns
    )
    assert result.total_excess == pytest.approx(direct_excess)
    assert brinson_attribution(
        portfolio_weights, benchmark_weights, portfolio_returns, benchmark_returns
    )["total_excess"] == pytest.approx(direct_excess)


def test_brinson_rejects_incomplete_or_unaligned_allocations() -> None:
    with pytest.raises(ValueError, match="sum to 1"):
        calculate_brinson([0.4, 0.4], [0.5, 0.5], [0.1, 0.0], [0.1, 0.0])
    with pytest.raises(ValueError, match="length"):
        calculate_brinson([0.5, 0.5], [1.0], [0.1, 0.0], [0.1, 0.0])


def test_registry_compares_multiple_benchmarks_and_active_share() -> None:
    registry = BenchmarkRegistry()
    registry.register("HS300", [0.01, -0.01, 0.02])
    registry.register("SPX", [0.005, -0.005, 0.01])

    metric = registry.compare(
        "HS300",
        [0.02, -0.005, 0.015],
        portfolio_weights=[0.6, 0.4],
        benchmark_weights=[0.5, 0.5],
    )
    all_metrics = registry.compare_all([0.02, -0.005, 0.015])

    assert metric.active_share == pytest.approx(0.1)
    assert metric.tracking_error > 0
    assert set(all_metrics) == {"HS300", "SPX"}


def test_attribution_report_keeps_dimensions_separate() -> None:
    effect = calculate_brinson([0.6, 0.4], [0.5, 0.5], [0.12, 0.03], [0.08, 0.04])
    report = AttributionReport(
        as_of=date(2026, 5, 25),
        frequency="daily",
        benchmark_id="HS300",
        effects_by_dimension={"style": effect, "security_selection": effect},
    )

    document = report.to_dict()
    assert document["benchmark_id"] == "HS300"
    assert set(document["effects_by_dimension"]) == {"security_selection", "style"}
    with pytest.raises(ValueError, match="unsupported"):
        AttributionReport(date.today(), "daily", "HS300", {"made_up": effect})
