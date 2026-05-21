"""Tests for the walk-forward (out-of-sample) validator."""

from __future__ import annotations

import pytest

from polymarket_edge.hl_backtest import FundingTick
from polymarket_edge.walkforward import (
    DAY_MS,
    HOUR_MS,
    walk_forward_top_k_trailing,
    walk_forward_top_k_trailing_net_spread,
)


def _grid(coin: str, fundings: list[float], start_ms: int = 0) -> list[FundingTick]:
    return [FundingTick(coin, start_ms + i * HOUR_MS, f) for i, f in enumerate(fundings)]


def test_window_construction_synthetic() -> None:
    """40 days, 2 coins, constant funding. With train=15/test=7/step=3 the
    sliding scheme starts at day 0,3,6,9,12,15,18 — but each window needs
    train_days+test_days=22 days of room, so the last viable start is when
    start+22 <= 40, i.e. start <= 18. Starts: 0,3,6,9,12,15,18 -> 7 windows.
    """
    hours = 40 * 24
    ticks = _grid("BTC", [0.0001] * hours) + _grid("ETH", [0.0002] * hours)
    result = walk_forward_top_k_trailing(
        ticks,
        train_days=15,
        test_days=7,
        step_days=3,
        top_k=1,
        trailing_hours=24,
        rebalance_hours=8,
    )
    assert result.n_windows == 7
    first = result.windows[0]
    assert first.train_start_ms == 0
    assert first.train_end_ms == 15 * DAY_MS
    assert first.test_start_ms == 15 * DAY_MS
    assert first.test_end_ms == 22 * DAY_MS
    # Each subsequent window shifts by step_days
    for prev, cur in zip(result.windows[:-1], result.windows[1:], strict=True):
        assert cur.train_start_ms - prev.train_start_ms == 3 * DAY_MS
        assert cur.test_start_ms - prev.test_start_ms == 3 * DAY_MS


def test_oos_equals_in_sample_when_strategy_constant() -> None:
    """Constant funding -> no selection dynamics, IS and OOS per-period returns
    are identical, so annualized returns must match to float tolerance."""
    hours = 30 * 24
    ticks = _grid("BTC", [0.0001] * hours) + _grid("ETH", [0.0001] * hours)
    result = walk_forward_top_k_trailing(
        ticks,
        train_days=10,
        test_days=5,
        step_days=3,
        top_k=1,
        trailing_hours=24,
        rebalance_hours=8,
    )
    assert result.n_windows >= 1
    for w in result.windows:
        assert w.in_sample_annualized == pytest.approx(
            w.out_of_sample_annualized, abs=1e-12
        )


def test_oos_decay_positive_on_overfit_data() -> None:
    """Engineered overfit signal: one coin's funding spikes high during the
    training segment, then crashes during the test segment. The trailing-mean
    predictor latches onto the high-funding coin from train and continues to
    pick it through the test segment as the trailing window still reflects
    the late-train spike — but the actual realized funding in test is negative.
    IS includes the train profits; OOS sees only the test crash. Expect
    OOS annualized return < IS annualized return.
    """
    train_hours = 20 * 24
    test_hours = 10 * 24
    spike = [0.001] * train_hours
    crash = [-0.001] * test_hours
    flat = [0.00005] * (train_hours + test_hours)
    ticks = (
        _grid("SPIKE", spike + crash)
        + _grid("FLAT_A", flat)
        + _grid("FLAT_B", flat)
    )
    result = walk_forward_top_k_trailing(
        ticks,
        train_days=15,
        test_days=10,
        step_days=5,
        top_k=1,
        trailing_hours=24,
        rebalance_hours=8,
    )
    assert result.n_windows >= 1
    # Aggregate: OOS should be meaningfully lower than IS on a setup designed
    # to over-fit the training segment.
    assert (
        result.out_of_sample_ann_ret_mean < result.in_sample_ann_ret_mean
    ), (
        f"OOS {result.out_of_sample_ann_ret_mean:+.4f} should be < "
        f"IS {result.in_sample_ann_ret_mean:+.4f}"
    )
    assert result.is_oos_decay_pp > 0


def test_step_days_zero_raises() -> None:
    ticks = _grid("BTC", [0.0001] * (40 * 24))
    with pytest.raises(ValueError, match="step_days"):
        walk_forward_top_k_trailing(
            ticks,
            train_days=10,
            test_days=5,
            step_days=0,
        )


def test_walkforward_returns_zero_windows_when_data_too_short() -> None:
    """Input shorter than train_days + test_days yields no windows, not a crash."""
    ticks = _grid("BTC", [0.0001] * (10 * 24))  # 10 days
    result = walk_forward_top_k_trailing(
        ticks,
        train_days=15,
        test_days=7,
        step_days=3,
    )
    assert result.n_windows == 0
    assert result.windows == []
    assert result.in_sample_ann_ret_mean == 0.0
    assert result.out_of_sample_ann_ret_mean == 0.0


def test_walkforward_empty_input() -> None:
    result = walk_forward_top_k_trailing([])
    assert result.n_windows == 0


def test_walkforward_net_spread_reduces_returns() -> None:
    """Net-of-cost walk-forward must produce returns <= gross on every window
    (the cost is subtracted symmetrically from each rebalance)."""
    hours = 30 * 24
    ticks = (
        _grid("BTC", [0.0002] * hours)
        + _grid("ETH", [0.00015] * hours)
        + _grid("SOL", [0.0001] * hours)
    )
    gross = walk_forward_top_k_trailing(
        ticks,
        train_days=10,
        test_days=5,
        step_days=5,
        top_k=2,
    )
    net = walk_forward_top_k_trailing_net_spread(
        ticks,
        train_days=10,
        test_days=5,
        step_days=5,
        top_k=2,
        spread_bps_per_leg=5.0,
    )
    assert gross.n_windows == net.n_windows
    assert gross.n_windows >= 1
    for g, n in zip(gross.windows, net.windows, strict=True):
        assert n.out_of_sample_annualized <= g.out_of_sample_annualized
        assert n.in_sample_annualized <= g.in_sample_annualized


def test_walkforward_train_days_negative_raises() -> None:
    ticks = _grid("BTC", [0.0001] * (40 * 24))
    with pytest.raises(ValueError, match="train_days"):
        walk_forward_top_k_trailing(ticks, train_days=0, test_days=5, step_days=3)
    with pytest.raises(ValueError, match="test_days"):
        walk_forward_top_k_trailing(ticks, train_days=10, test_days=0, step_days=3)
