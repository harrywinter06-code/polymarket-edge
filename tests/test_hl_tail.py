"""Tests for the tail-risk module (`hl_tail`)."""

from __future__ import annotations

import random

import pytest

from polymarket_edge.hl_tail import tail_stats


def test_tail_stats_empty_returns_zeros() -> None:
    """Empty input must return all-zero stats, not crash."""
    ts = tail_stats([], hours_per_period=8)
    assert ts.n_periods == 0
    assert ts.var_95 == 0.0
    assert ts.expected_shortfall_95 == 0.0
    assert ts.max_drawdown == 0.0
    assert ts.max_drawdown_periods == 0


def test_tail_stats_constant_returns() -> None:
    """Constant 0.001 returns: VaR = ES = 0.001 (no variation), drawdown = 0."""
    constant = [0.001] * 30
    ts = tail_stats(constant, hours_per_period=8)
    assert ts.var_95 == pytest.approx(0.001, abs=1e-12)
    assert ts.var_99 == pytest.approx(0.001, abs=1e-12)
    assert ts.expected_shortfall_95 == pytest.approx(0.001, abs=1e-12)
    assert ts.expected_shortfall_99 == pytest.approx(0.001, abs=1e-12)
    assert ts.max_drawdown == 0.0
    assert ts.max_drawdown_periods == 0
    assert ts.n_drawdown_periods == 0
    assert ts.drawdown_recovery_periods == 0
    assert ts.worst_period_return == pytest.approx(0.001)
    assert ts.best_period_return == pytest.approx(0.001)


def test_tail_stats_known_worst_5_percent() -> None:
    """Engineered series where the worst 5% are exactly one element.

    With n=20 returns -5..14, the 5th-percentile (linear interp) sits between
    elements 0 (-5) and 1 (-4): pct=5 -> k = 19 * 0.05 = 0.95 -> interp =
    -5 * 0.05 + -4 * 0.95 = -4.05. Only element 0 (-5) is <= -4.05, so
    ES_95 = mean([-5]) = -5.
    """
    returns = [float(x) for x in range(-5, 15)]  # -5, -4, ..., 14 (n=20)
    ts = tail_stats(returns, hours_per_period=8)
    expected_var_95 = -5.0 * 0.05 + -4.0 * 0.95
    assert ts.var_95 == pytest.approx(expected_var_95, abs=1e-12)
    assert ts.expected_shortfall_95 == pytest.approx(-5.0, abs=1e-12)
    # ES must be <= VaR (more negative or equal).
    assert ts.expected_shortfall_95 <= ts.var_95
    assert ts.worst_period_return == -5.0
    assert ts.best_period_return == 14.0


def test_es_below_var_for_skewed_series() -> None:
    """ES must always be <= VaR (more-negative-or-equal). Verify on a
    deliberately fat-tailed series."""
    rng = random.Random(7)
    returns = [rng.gauss(0.001, 0.002) for _ in range(200)]
    returns += [-0.05, -0.04, -0.03]  # inject left-tail outliers
    ts = tail_stats(returns, hours_per_period=8)
    assert ts.expected_shortfall_95 <= ts.var_95
    assert ts.expected_shortfall_99 <= ts.var_99


def test_drawdown_depth_known() -> None:
    """+1, +1, -3, +1 -> cumulative 1, 2, -1, 0; peak=2, trough=-1, DD=3.0."""
    ts = tail_stats([1.0, 1.0, -3.0, 1.0], hours_per_period=8)
    assert ts.max_drawdown == pytest.approx(3.0, abs=1e-12)


def test_drawdown_duration_recovers() -> None:
    """Drawdown that recovers fully: +1, -0.5, -0.5, +1.

    Cumulative: 1, 0.5, 0.0, 1.0. Peak set at i=0 (cum=1); periods 1 and 2
    are below peak; period 3 returns to peak.
      - max_dd_periods = 2 (longest below-peak run)
      - drawdown_recovery_periods = 2 (from trough at i=2 back to peak at i=3
        ... wait, the trough is at i=2 (cum=0). Recovery to peak (1.0) at i=3.
        That's j - trough_index = 3 - 2 = 1 period.
    """
    ts = tail_stats([1.0, -0.5, -0.5, 1.0], hours_per_period=8)
    assert ts.max_drawdown == pytest.approx(1.0, abs=1e-12)
    assert ts.max_drawdown_periods == 2
    assert ts.n_drawdown_periods == 2
    assert ts.drawdown_recovery_periods == 1


def test_drawdown_duration_long_run_then_recovery() -> None:
    """5-period below-peak run that recovers. Cumulative checked manually.

    Returns: +2, -0.5, -0.5, -0.5, -0.5, -0.5, +2.5
    Cum:      2, 1.5, 1.0, 0.5, 0.0, -0.5, +2.0
    Peak set at i=0 (2). Periods 1..5 are all below peak (5 periods).
    Trough at i=5 (cum=-0.5). Recovery: i=6 -> cum=2.0 >= 2.0. Recovery = 1.
    max_dd_periods should be 5. n_dd_periods = 5. max_dd = 2.5.
    """
    ts = tail_stats(
        [2.0, -0.5, -0.5, -0.5, -0.5, -0.5, 2.5], hours_per_period=8
    )
    assert ts.max_drawdown == pytest.approx(2.5, abs=1e-12)
    assert ts.max_drawdown_periods == 5
    assert ts.n_drawdown_periods == 5
    assert ts.drawdown_recovery_periods == 1


def test_drawdown_duration_no_recovery() -> None:
    """Series ends still underwater. Recovery sentinel = 0."""
    # Cum: 1, 2, 1, 0, -1, -2 — peak at i=1 (2), trough at i=5 (-2), no recovery.
    ts = tail_stats([1.0, 1.0, -1.0, -1.0, -1.0, -1.0], hours_per_period=8)
    assert ts.max_drawdown == pytest.approx(4.0, abs=1e-12)
    assert ts.drawdown_recovery_periods == 0
    # Periods 2, 3, 4, 5 are below peak — 4 periods.
    assert ts.max_drawdown_periods == 4
    assert ts.n_drawdown_periods == 4


def test_var_99_more_extreme_than_var_95() -> None:
    """On any return series with variation, var_99 <= var_95 (more negative)."""
    rng = random.Random(11)
    returns = [rng.gauss(0.0, 0.01) for _ in range(500)]
    ts = tail_stats(returns, hours_per_period=8)
    assert ts.var_99 <= ts.var_95
    assert ts.expected_shortfall_99 <= ts.expected_shortfall_95


def test_annualized_return_matches_simple_formula() -> None:
    """annualized_return = mean(returns) * (HOURS_PER_YEAR / hours_per_period)."""
    returns = [0.001, 0.002, -0.0005, 0.0015, 0.0008]
    ts = tail_stats(returns, hours_per_period=8)
    mean = sum(returns) / len(returns)
    expected = mean * ((24 * 365) / 8)
    assert ts.annualized_return == pytest.approx(expected, abs=1e-12)


def test_n_periods_and_hours_per_period_recorded() -> None:
    """Trivial provenance: TailStats echoes back its inputs."""
    ts = tail_stats([0.001] * 17, hours_per_period=4)
    assert ts.n_periods == 17
    assert ts.hours_per_period == 4
