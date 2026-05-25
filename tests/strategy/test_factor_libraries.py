import numpy as np
from quant_trading.strategy import enhanced_factors as enhanced
from quant_trading.strategy import extended_factors as extended
from quant_trading.strategy.factors import compute_factor_scores, factor_macd, rank_stocks


def _market_series(length: int = 300) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    x = np.linspace(0, 10 * np.pi, length)
    closes = 100 + np.linspace(0, 18, length) + 2 * np.sin(x)
    volumes = 1_000_000 + 100_000 * (1 + np.cos(x))
    amounts = closes * volumes
    return closes, volumes, amounts


def test_basic_factor_model_produces_macd_and_cross_section_ranking() -> None:
    closes, volumes, _ = _market_series()
    scores = compute_factor_scores(closes, volumes)
    assert np.isfinite(factor_macd(closes))
    assert any(np.isfinite(value) for value in scores.values())

    universe = {}
    for i in range(6):
        universe[f"S{i}"] = {"close": closes * (1 + i * 0.002), "volume": volumes * (1 + i * 0.03)}
    ranked = rank_stocks(universe)
    assert len(ranked) == 6
    assert ranked == sorted(ranked, key=lambda item: item[1], reverse=True)


def test_enhanced_price_volume_and_value_factors_are_finite() -> None:
    closes, volumes, _ = _market_series()
    highs = closes + 1
    lows = closes - 1
    assert enhanced.factor_pe_percentile(12.0) < 0
    assert np.isfinite(enhanced.factor_turnover_anomaly(volumes))
    assert np.isfinite(enhanced.factor_money_flow(closes, highs, lows, closes, volumes))
    assert np.isfinite(enhanced.factor_volume_price_trend(closes, volumes))
    assert np.isfinite(enhanced.factor_relative_strength(closes))


def test_extended_factor_library_covers_volatility_liquidity_quality_and_flow() -> None:
    closes, volumes, amounts = _market_series()
    highs = closes + 1
    lows = closes - 1
    pe = np.linspace(10, 14, len(closes))
    turnover = volumes / volumes.max()

    outputs = [
        extended.value_ep(12),
        extended.momentum_12m_1m(closes),
        extended.factor_macd_hist(closes),
        extended.realized_vol_1m(closes),
        extended.max_daily_return(closes),
        extended.return_skewness(closes),
        extended.amihud_illiquidity(closes, amounts),
        extended.earnings_stability(pe),
        extended.gross_profitability_proxy(closes),
        extended.log_market_cap_proxy(amounts, turnover),
        extended.chaikin_money_flow(highs, lows, closes, volumes),
    ]
    assert len(extended.FACTOR_SPECS) >= 29
    assert all(np.isfinite(output) for output in outputs)
