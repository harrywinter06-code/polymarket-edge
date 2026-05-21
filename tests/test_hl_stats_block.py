"""Tests for the block-bootstrap CI module (`hl_stats_block`)."""

from __future__ import annotations

import random

import pytest

from polymarket_edge.hl_stats import bootstrap_backtest_stats
from polymarket_edge.hl_stats_block import (
    estimate_optimal_block_length,
    moving_block_bootstrap,
    stationary_bootstrap,
)


def _ar1(n: int, phi: float, sigma: float = 1.0, seed: int = 0) -> list[float]:
    """Generate an AR(1) series x_t = phi * x_{t-1} + eps_t, eps ~ N(0, sigma^2)."""
    rng = random.Random(seed)
    out: list[float] = []
    x = 0.0
    # Burn-in to forget the zero initial condition.
    for _ in range(200):
        x = phi * x + rng.gauss(0.0, sigma)
    for _ in range(n):
        x = phi * x + rng.gauss(0.0, sigma)
        out.append(x)
    return out


def test_optimal_block_length_returns_at_least_one() -> None:
    """Selector always returns a positive integer, even on degenerate inputs."""
    assert estimate_optimal_block_length([]) >= 1
    assert estimate_optimal_block_length([0.5]) >= 1
    assert estimate_optimal_block_length([0.5, 0.5, 0.5, 0.5]) >= 1


def test_optimal_block_length_on_uncorrelated_returns_to_one() -> None:
    """IID Gaussian has ACF that dies at lag 1; selector should pick the
    shortest sensible block."""
    rng = random.Random(123)
    iid = [rng.gauss(0.0, 1.0) for _ in range(200)]
    bl = estimate_optimal_block_length(iid)
    # Allow lag=2 as a sampling-noise tolerance; the assertion is "short".
    assert bl <= 2, f"expected block_length<=2 on IID Gaussian, got {bl}"


def test_optimal_block_length_on_strongly_autocorrelated_at_least_three() -> None:
    """An AR(1) with phi=0.7 has |acf(k)| = 0.7^k, which crosses the
    2/sqrt(n) bound at a non-trivial lag for n in the hundreds. The selector
    should pick at least 3."""
    series = _ar1(n=300, phi=0.7, seed=42)
    bl = estimate_optimal_block_length(series)
    assert bl >= 3, f"expected block_length>=3 on AR(1) phi=0.7, got {bl}"


def test_moving_block_zero_variance_returns_zero_width_ci() -> None:
    """Constant returns can only resample to themselves -> zero-width CI."""
    constant = [0.001] * 30
    stats = moving_block_bootstrap(
        constant, hours_per_period=8, block_length=4, n_resamples=500, seed=7
    )
    ar = stats.annualized_return
    assert ar.ci_high - ar.ci_low < 1e-12
    assert ar.ci_low == pytest.approx(ar.point, abs=1e-12)
    assert ar.ci_high == pytest.approx(ar.point, abs=1e-12)
    s = stats.sharpe
    assert s.point == 0.0
    assert s.ci_low == 0.0
    assert s.ci_high == 0.0


def test_moving_block_ci_covers_point_estimate() -> None:
    """The percentile CI must enclose the point estimate on both stats."""
    rng = random.Random(11)
    returns = [rng.gauss(0.001, 0.0005) for _ in range(80)]
    stats = moving_block_bootstrap(
        returns, hours_per_period=8, block_length=4, n_resamples=2000, seed=42
    )
    ar = stats.annualized_return
    assert ar.ci_low <= ar.point <= ar.ci_high
    s = stats.sharpe
    assert s.ci_low <= s.point <= s.ci_high


def test_stationary_ci_is_wider_than_iid_on_autocorrelated_data() -> None:
    """The bug-or-not-bug test: on persistently autocorrelated returns, the
    stationary bootstrap should produce a wider annualized-return CI than the
    naive IID bootstrap. This is the entire reason this module exists.

    We use AR(1) with phi=0.7 — strong but realistic regime persistence.
    """
    # Scale to a plausible per-period return magnitude (bps-range) so the
    # annualized statistic doesn't underflow.
    raw = _ar1(n=200, phi=0.7, sigma=1.0, seed=7)
    returns = [r * 0.001 + 0.0005 for r in raw]

    iid_stats = bootstrap_backtest_stats(
        returns, hours_per_period=8, n_resamples=2000, seed=1
    )
    block_stats = stationary_bootstrap(
        returns, hours_per_period=8, block_length=5.0, n_resamples=2000, seed=1
    )

    iid_width = iid_stats.annualized_return.ci_high - iid_stats.annualized_return.ci_low
    block_width = (
        block_stats.annualized_return.ci_high - block_stats.annualized_return.ci_low
    )
    assert block_width >= iid_width, (
        f"stationary block bootstrap CI width ({block_width:.6f}) should be >= "
        f"IID CI width ({iid_width:.6f}) on strongly autocorrelated returns"
    )
