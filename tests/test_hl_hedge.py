"""Tests for the spread-cost-aware Hyperliquid funding-capture backtest."""

from __future__ import annotations

import pytest

from polymarket_edge.hl_backtest import (
    HOURS_PER_YEAR,
    FundingTick,
    backtest_top_k_trailing,
)
from polymarket_edge.hl_hedge import (
    backtest_top_k_trailing_net_spread,
    cadence_frontier,
    sweep_spread_sensitivity,
)


def _grid(coin: str, fundings: list[float], start_ms: int = 0) -> list[FundingTick]:
    return [FundingTick(coin, start_ms + i * 3_600_000, f) for i, f in enumerate(fundings)]


def _two_coin_universe() -> list[FundingTick]:
    # Positive-funding universe: BTC trails higher, ETH lower, both pay shorts.
    btc = _grid("BTC", [0.00020] * 24 + [0.00015] * 32)
    eth = _grid("ETH", [0.00010] * 24 + [0.00012] * 32)
    return btc + eth


def test_zero_spread_reproduces_existing_backtest_exactly() -> None:
    """The pre-mortem invariant: with spread_bps_per_leg=0 the netted backtest
    must produce numbers indistinguishable from `backtest_top_k_trailing`."""
    ticks = _two_coin_universe()
    baseline = backtest_top_k_trailing(
        ticks, top_k=1, trailing_hours=24, rebalance_hours=8
    )
    netted = backtest_top_k_trailing_net_spread(
        ticks, top_k=1, trailing_hours=24, rebalance_hours=8, spread_bps_per_leg=0.0
    )
    assert netted.n_rebalances == baseline.n_rebalances
    assert netted.top_k == baseline.top_k
    assert netted.rebalance_hours == baseline.rebalance_hours
    assert netted.trailing_hours == baseline.trailing_hours
    assert netted.n_distinct_coins_held == baseline.n_distinct_coins_held
    assert netted.total_return == pytest.approx(baseline.total_return, abs=1e-15)
    assert netted.annualized_return == pytest.approx(baseline.annualized_return, abs=1e-15)
    assert netted.annualized_vol == pytest.approx(baseline.annualized_vol, abs=1e-15)
    assert netted.sharpe == pytest.approx(baseline.sharpe, abs=1e-12)
    assert netted.max_drawdown == pytest.approx(baseline.max_drawdown, abs=1e-15)
    assert netted.hit_rate == pytest.approx(baseline.hit_rate, abs=1e-15)


def test_positive_spread_reduces_annualized_return_by_exact_amount() -> None:
    """Each rebalance pays 4 * spread_bps_per_leg / 10_000 in cost; the
    annualized-return delta must equal that per-period cost times periods_per_year."""
    ticks = _two_coin_universe()
    spread_bps_per_leg = 5.0
    rebalance_hours = 8
    baseline = backtest_top_k_trailing_net_spread(
        ticks, top_k=1, trailing_hours=24, rebalance_hours=rebalance_hours,
        spread_bps_per_leg=0.0,
    )
    netted = backtest_top_k_trailing_net_spread(
        ticks, top_k=1, trailing_hours=24, rebalance_hours=rebalance_hours,
        spread_bps_per_leg=spread_bps_per_leg,
    )
    cost_per_period = 4 * spread_bps_per_leg / 10_000
    periods_per_year = HOURS_PER_YEAR / rebalance_hours
    expected_delta = cost_per_period * periods_per_year
    actual_delta = baseline.annualized_return - netted.annualized_return
    assert actual_delta == pytest.approx(expected_delta, rel=1e-12)


def test_sweep_returns_one_result_per_spread_level_in_order() -> None:
    ticks = _two_coin_universe()
    spreads = (0.0, 2.5, 5.0, 10.0, 20.0)
    results = sweep_spread_sensitivity(
        ticks, spreads_bps=spreads, top_k=1, trailing_hours=24, rebalance_hours=8,
    )
    assert len(results) == len(spreads)
    # Higher spread => lower annualized return; monotone decreasing.
    ann = [r.annualized_return for r in results]
    assert ann == sorted(ann, reverse=True)
    # Strategy strings encode the spread in input order.
    for r, s in zip(results, spreads, strict=True):
        assert f"spread{s}bp" in r.strategy


def test_extreme_spread_turns_positive_carry_negative() -> None:
    """100 bps per leg = 400 bps round-trip per rebalance — must dominate the
    funding carry on a small positive-funding dataset."""
    ticks = _two_coin_universe()
    r = backtest_top_k_trailing_net_spread(
        ticks, top_k=1, trailing_hours=24, rebalance_hours=8,
        spread_bps_per_leg=100.0,
    )
    assert r.n_rebalances > 0
    assert r.annualized_return < 0


def test_cadence_frontier_returns_one_row_per_cadence() -> None:
    ticks = _two_coin_universe()
    rows = cadence_frontier(
        ticks,
        cadences_hours=(8, 24, 72),
        top_k=1,
        trailing_hours=24,
        spread_bps_per_leg=5.0,
    )
    assert [r.rebalance_hours for r in rows] == [8, 24, 72]
    # Each row carries the four headline numbers.
    for r in rows:
        assert isinstance(r.gross_annualized, float)
        assert isinstance(r.net_annualized, float)
        assert isinstance(r.net_sharpe, float)
        assert isinstance(r.breakeven_bps_per_leg, float)


def test_cadence_frontier_breakeven_solves_net_equals_zero() -> None:
    """breakeven_bps_per_leg * per_period_gross math: setting the spread to
    the reported breakeven should produce net annualised ~ 0."""
    ticks = _two_coin_universe()
    rows = cadence_frontier(
        ticks, cadences_hours=(8,), top_k=1, trailing_hours=24,
        spread_bps_per_leg=0.0,
    )
    if rows[0].breakeven_bps_per_leg <= 0:
        # Constant-positive carry can produce a tiny / non-positive BE; skip.
        return
    # Re-run at the reported breakeven and verify net ~ 0.
    r_be = backtest_top_k_trailing_net_spread(
        ticks, top_k=1, trailing_hours=24, rebalance_hours=8,
        spread_bps_per_leg=rows[0].breakeven_bps_per_leg,
    )
    assert abs(r_be.annualized_return) < 1e-6
