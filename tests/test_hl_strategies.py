"""Tests for the alternative funding-capture strategies (momentum)."""

from __future__ import annotations

import pytest

from polymarket_edge.hl_backtest import FundingTick
from polymarket_edge.hl_strategies import backtest_funding_momentum


def _grid(coin: str, fundings: list[float], start_ms: int = 0) -> list[FundingTick]:
    return [FundingTick(coin, start_ms + i * 3_600_000, f) for i, f in enumerate(fundings)]


def test_momentum_picks_rising_coin_over_high_level() -> None:
    """Two coins:
      - STEADY: constant high funding. Long-window pstdev = 0 -> excluded
        from ranking (degenerate z-score).
      - RISING: low baseline for the long window, then a high short window
        immediately before rebalance. Positive z -> selected.

    With long=48h, short=8h, rebal=8h: i starts at 48. The long window is
    hours [0, 48), the short window is hours [40, 48), the realized future
    is hours [48, 56). Build the series so the future for RISING is high
    funding and verify the top-1 momentum pick captures it.
    """
    # 48h long window + 8h future = 56 hourly ticks per coin.
    # STEADY: constant 0.0010 throughout -> pstdev(long)=0, excluded.
    steady = _grid("STEADY", [0.0010] * 56)
    # RISING: hours [0, 40) baseline at 0.00005; hours [40, 48) ramp to 0.0005;
    # hours [48, 56) realized future at 0.0008 (the high-funding payoff).
    rising_funding = [0.00005] * 40 + [0.0005] * 8 + [0.0008] * 8
    rising = _grid("RISING", rising_funding)
    ticks = steady + rising

    r = backtest_funding_momentum(
        ticks,
        top_k=1,
        short_window_hours=8,
        long_window_hours=48,
        rebalance_hours=8,
    )
    assert r.n_rebalances == 1
    assert r.n_distinct_coins_held == 1
    # The single held coin should be RISING; total return = 8 * 0.0008 = 0.0064.
    assert r.total_return == pytest.approx(8 * 0.0008, abs=1e-12)


def test_momentum_zero_when_history_too_short() -> None:
    """If the input is shorter than long_window_hours + rebalance_hours,
    the strategy produces zero rebalances."""
    # long=168, rebal=8 -> need 176 ticks. Provide 100.
    btc = _grid("BTC", [0.0001] * 100)
    eth = _grid("ETH", [0.0002] * 100)
    r = backtest_funding_momentum(
        btc + eth,
        top_k=1,
        short_window_hours=24,
        long_window_hours=168,
        rebalance_hours=8,
    )
    assert r.n_rebalances == 0
    assert r.total_return == 0.0


def test_momentum_rejects_bad_windows() -> None:
    """Sanity: short > long is rejected; non-positive windows are rejected."""
    btc = _grid("BTC", [0.0001] * 200)
    with pytest.raises(ValueError):
        backtest_funding_momentum(
            btc, short_window_hours=48, long_window_hours=24, rebalance_hours=8
        )
    with pytest.raises(ValueError):
        backtest_funding_momentum(
            btc, short_window_hours=0, long_window_hours=24, rebalance_hours=8
        )


def test_momentum_summary_uses_long_window_as_trailing_hours() -> None:
    """The returned BacktestResult should report `trailing_hours == long_window_hours`
    so the result is comparable to a trailing-mean backtest in downstream tables."""
    btc = _grid("BTC", [0.0001 + 0.00001 * i for i in range(200)])
    eth = _grid("ETH", [0.0001 + 0.00002 * i for i in range(200)])
    r = backtest_funding_momentum(
        btc + eth,
        top_k=1,
        short_window_hours=24,
        long_window_hours=72,
        rebalance_hours=8,
    )
    assert r.trailing_hours == 72
    assert r.rebalance_hours == 8
    assert r.top_k == 1
