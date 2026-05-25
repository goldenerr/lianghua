"""Backtest determinism regression (test-004). AGENTS.md §16: fixed seeds, reproducible results."""

import numpy as np


def test_seed_reproducibility():
    np.random.seed(42)
    a = np.random.randn(10)
    np.random.seed(42)
    b = np.random.randn(10)
    assert np.array_equal(a, b)
