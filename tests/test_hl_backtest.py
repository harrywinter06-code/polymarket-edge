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


def test_survivorship_aware_grid_includes_short_listed_coin() -> None:
    """The grid is the *union* of timestamps; a coin missing the late slice
    of the window doesn't drop the late buckets for the coins that do have
    data. Eligibility is enforced per-bucket inside the strategy loop.

    Setup: BTC has 32 hourly ticks; ETH has only the first 28 (delisted, or
    arrived late). At rebalance i=24 the future window is hours [24, 32).
    BTC has full future-window data; ETH does not. The strategy holds top-2
    by trailing mean (both qualify on the trailing window [0, 24)), but the
    realised P&L is averaged over only the coins that have full future data
    — so the rebalance counts and contributes BTC's funding (0.0008).
    """
    btc = _grid("BTC", [0.0001] * 32)
    eth = _grid("ETH", [0.0002] * 28)
    r = backtest_top_k_trailing(
        btc + eth, top_k=2, trailing_hours=24, rebalance_hours=8
    )
    assert r.n_rebalances == 1
    # ETH had higher trailing mean and is held, but only BTC has future data.
    # Realised P&L is BTC's 8h sum (0.0008) averaged over the 1 coin that
    # actually had the future window.
    assert abs(r.total_return - 0.0008) < 1e-12


def test_survivorship_correction_doesnt_inflate_when_coin_arrives_late() -> None:
    """If a high-funding coin is only available for the tail of the window,
    earlier rebalances must NOT see it (no look-ahead) and later rebalances
    must include it as a candidate when its trailing window is complete.

    BTC: 64 hours of 0.0001. NEW: hours [40, 64) of 0.0010.

    With trailing_hours=24, rebalance_hours=8, top_k=1:
      - i=24: NEW has 0 trailing-window ticks (window [0, 24)); only BTC qualifies.
      - i=32: NEW has 0 ticks in window [8, 32) until hour 40 — still doesn't qualify.
      - i=48: NEW has trailing window [24, 48) with 8 ticks (hours 40-47), not 24
              -> doesn't qualify (require full trailing window).
      - i=56: NEW has [32, 56) with 16 ticks -> doesn't qualify.
      - At no point does NEW qualify for top-1 — BTC wins every rebalance.
    """
    btc = _grid("BTC", [0.0001] * 64)
    new_coin = [
        FundingTick("NEW", i * 3_600_000, 0.0010)
        for i in range(40, 64)
    ]
    r = backtest_top_k_trailing(
        btc + new_coin, top_k=1, trailing_hours=24, rebalance_hours=8
    )
    # All held positions should be BTC; NEW never qualifies for trailing.
    assert r.n_distinct_coins_held == 1
    # Five rebalance buckets fit in 64 hours minus 24 trailing.
    assert r.n_rebalances == 5
