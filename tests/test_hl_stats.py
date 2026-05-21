"""Tests for the bootstrap-CI module."""

from __future__ import annotations

import random

import pytest

from polymarket_edge.hl_backtest import FundingTick, backtest_top_k_trailing
from polymarket_edge.hl_stats import (
    bootstrap_backtest_stats,
    compute_per_period_returns_trailing,
)


def _grid(coin: str, fundings: list[float], start_ms: int = 0) -> list[FundingTick]:
    return [FundingTick(coin, start_ms + i * 3_600_000, f) for i, f in enumerate(fundings)]


def test_bootstrap_zero_variance_returns_zero_ci_width() -> None:
    """A constant return series can only resample to itself; the CI must collapse
    to the point estimate (width < 1e-12)."""
    constant = [0.001] * 30
    stats = bootstrap_backtest_stats(
        constant, hours_per_period=8, n_resamples=500, seed=7
    )
    ar = stats.annualized_return
    assert ar.ci_high - ar.ci_low < 1e-12
    assert ar.ci_low == pytest.approx(ar.point, abs=1e-12)
    assert ar.ci_high == pytest.approx(ar.point, abs=1e-12)
    # Constant returns -> stdev = 0 -> Sharpe = 0 by the base-backtest fallback.
    s = stats.sharpe
    assert s.point == 0.0
    assert s.ci_low == 0.0
    assert s.ci_high == 0.0


def test_bootstrap_ci_covers_point_estimate() -> None:
    """Generic invariant: percentile bootstrap of a symmetric-ish distribution
    must enclose the point estimate on both stats."""
    rng = random.Random(123)
    returns = [rng.gauss(0.001, 0.0005) for _ in range(60)]
    stats = bootstrap_backtest_stats(
        returns, hours_per_period=8, n_resamples=2000, seed=42
    )
    ar = stats.annualized_return
    assert ar.ci_low < ar.point < ar.ci_high
    s = stats.sharpe
    assert s.ci_low < s.point < s.ci_high


def test_bootstrap_n_resamples_respected() -> None:
    """The returned BacktestStats must report the requested n_resamples."""
    returns = [0.001, 0.002, -0.0005, 0.0015, 0.0008] * 4
    stats = bootstrap_backtest_stats(
        returns, hours_per_period=8, n_resamples=137, seed=1
    )
    assert stats.n_resamples == 137


def test_compute_per_period_returns_matches_aggregate_backtest() -> None:
    """The helper that re-runs the trailing loop must produce a returns vector
    whose sum / count exactly reproduces the aggregate `BacktestResult`."""
    btc = _grid("BTC", [0.0001] * 24 + [0.0001] * 8)
    eth = _grid("ETH", [0.0002] * 24 + [0.0001] * 8)
    ticks = btc + eth
    per_period = compute_per_period_returns_trailing(
        ticks, top_k=1, trailing_hours=24, rebalance_hours=8
    )
    aggregate = backtest_top_k_trailing(
        ticks, top_k=1, trailing_hours=24, rebalance_hours=8
    )
    assert len(per_period) == aggregate.n_rebalances
    assert sum(per_period) == pytest.approx(aggregate.total_return, abs=1e-15)


def test_bootstrap_empty_returns_safely() -> None:
    """No data -> zero-everything stats, no crash."""
    stats = bootstrap_backtest_stats(
        [], hours_per_period=8, n_resamples=100, seed=1
    )
    assert stats.annualized_return.point == 0.0
    assert stats.annualized_return.ci_low == 0.0
    assert stats.annualized_return.ci_high == 0.0
    assert stats.sharpe.point == 0.0
