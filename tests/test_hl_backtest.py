"""Tests for the Hyperliquid funding-capture backtest engine."""

from __future__ import annotations

from polymarket_edge.hl_backtest import (
    FundingTick,
    backtest_passive,
    backtest_perfect_hindsight,
    backtest_top_k_trailing,
)


def _grid(coin: str, fundings: list[float], start_ms: int = 0) -> list[FundingTick]:
    return [FundingTick(coin, start_ms + i * 3_600_000, f) for i, f in enumerate(fundings)]


def test_passive_short_btc_sums_funding() -> None:
    # 24 hourly funding values of 0.0001 each, rebalance_hours=8 -> 3 periods of 0.0008
    ticks = _grid("BTC", [0.0001] * 24)
    r = backtest_passive(ticks, coin="BTC", rebalance_hours=8)
    assert r.n_rebalances == 3
    assert abs(r.total_return - 3 * 0.0008) < 1e-12
    assert r.hit_rate == 1.0


def test_passive_loses_on_negative_funding() -> None:
    ticks = _grid("BTC", [-0.0002] * 16)
    r = backtest_passive(ticks, coin="BTC", rebalance_hours=8)
    assert r.n_rebalances == 2
    assert r.total_return < 0
    assert r.hit_rate == 0.0


def test_perfect_hindsight_picks_highest_per_period() -> None:
    # Two coins, two 8-hour windows
    # Period 1 (hours 0-7): BTC = 0.0001 * 8 = 0.0008; ETH = 0.0002 * 8 = 0.0016 -> pick ETH
    # Period 2 (hours 8-15): BTC = 0.0003 * 8 = 0.0024; ETH = 0.0001 * 8 = 0.0008 -> pick BTC
    btc = _grid("BTC", [0.0001] * 8 + [0.0003] * 8)
    eth = _grid("ETH", [0.0002] * 8 + [0.0001] * 8)
    r = backtest_perfect_hindsight(btc + eth, top_k=1, rebalance_hours=8)
    assert r.n_rebalances == 2
    assert abs(r.total_return - (0.0016 + 0.0024)) < 1e-12
    assert r.n_distinct_coins_held == 2


def test_trailing_predictor_uses_only_past_data() -> None:
    # Two coins.
    # Hours 0-23 (trailing): BTC mean = 0.0001, ETH mean = 0.0002 -> ETH wins.
    # Hours 24-31 (forward): both funding 0.0001 each -> per-coin realized 0.0008.
    # Strategy with top_k=1 shorts ETH -> P&L = 0.0008.
    btc = _grid("BTC", [0.0001] * 24 + [0.0001] * 8)
    eth = _grid("ETH", [0.0002] * 24 + [0.0001] * 8)
    r = backtest_top_k_trailing(
        btc + eth, top_k=1, trailing_hours=24, rebalance_hours=8
    )
    assert r.n_rebalances == 1
    assert abs(r.total_return - 0.0008) < 1e-12


def test_trailing_predictor_zero_when_history_too_short() -> None:
    btc = _grid("BTC", [0.0001] * 10)
    r = backtest_top_k_trailing(btc, top_k=1, trailing_hours=24, rebalance_hours=8)
    assert r.n_rebalances == 0


def test_drawdown_captured() -> None:
    # Returns: +0.01, +0.01, -0.05, +0.01 -> peak after 2nd, drawdown after 3rd = 0.05
    ticks = _grid("BTC", [0.01 / 8] * 8 + [0.01 / 8] * 8 + [-0.05 / 8] * 8 + [0.01 / 8] * 8)
    r = backtest_passive(ticks, coin="BTC", rebalance_hours=8)
    assert r.n_rebalances == 4
    assert abs(r.max_drawdown - 0.05) < 1e-12


def test_sharpe_zero_on_constant_returns() -> None:
    ticks = _grid("BTC", [0.0001] * 16)
    r = backtest_passive(ticks, coin="BTC", rebalance_hours=8)
    assert r.annualized_vol == 0.0
    assert r.sharpe == 0.0


def test_grid_intersection_skips_periods_lacking_data_for_any_held_coin() -> None:
    """Sanity-check that the common-timestamp-grid construction guarantees
    every held coin has data over the rebalance window, so the per-coin
    completeness check in the loop is defensive but not load-bearing.

    Setup: BTC has 32 hours; ETH has 28. The intersection grid has 28 ticks;
    at i=24 the requested future window is grid[24:32] = 8 ticks but only 4
    are available, so the outer loop's `i + rebalance_hours <= len(grid)`
    guard rejects the period entirely. No partial-data inflation can occur.
    """
    btc = _grid("BTC", [0.0001] * 32)
    eth = _grid("ETH", [0.0002] * 28)
    r = backtest_top_k_trailing(
        btc + eth, top_k=2, trailing_hours=24, rebalance_hours=8
    )
    assert r.n_rebalances == 0
    assert r.total_return == 0.0
