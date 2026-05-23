"""Randomised property tests for hot-path math.

Each property is checked against a fixed-seed PRNG so failures reproduce.
The invariants are the ones called out in the original test-coverage
analysis as cheap-to-add and catch-real-bugs:

  - `walk_side` never reports consumed_notional_usd above the target.
  - `walk_side` only reports `book_exhausted` when the cumulative depth
    is genuinely below the target.
  - `align_series` is monotone in t_ms.
  - `persistence_stats` is invariant under a uniform time shift.
  - `forward_test` n_entries is monotone-decreasing in `threshold`.
"""

from __future__ import annotations

import itertools
import math
import random
from datetime import UTC, datetime, timedelta

import pytest

from polymarket_edge.analysis import (
    Trajectory,
    forward_test,
    persistence_stats,
)
from polymarket_edge.book_depth import Level, walk_side
from polymarket_edge.cross_venue import align_series

BUCKET_MS = 12 * 3_600_000


def _seeds() -> list[int]:
    return [20260101, 20260315, 20260520, 7, 99]


# ---------------------------------------------------------------------------
# walk_side
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", _seeds())
def test_walk_side_never_overconsumes(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(40):
        n_levels = rng.randint(0, 20)
        levels = [
            Level(price=rng.uniform(0.01, 0.99), size=rng.uniform(0.0, 500.0))
            for _ in range(n_levels)
        ]
        target = rng.uniform(0.0, 1000.0)
        r = walk_side(levels, target)
        # Floating-point slack: consumed should never exceed target by more
        # than rounding noise in the partial-fill arithmetic.
        assert r.consumed_notional_usd <= target + 1e-6


@pytest.mark.parametrize("seed", _seeds())
def test_walk_side_exhausted_iff_depth_below_target(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(40):
        levels = [
            Level(price=max(rng.uniform(-0.1, 0.99), 0.0), size=rng.uniform(0.0, 100.0))
            for _ in range(rng.randint(0, 15))
        ]
        target = rng.uniform(0.1, 500.0)
        r = walk_side(levels, target)
        total_depth = sum(lvl.price * lvl.size for lvl in levels if lvl.price > 0)
        if r.book_exhausted:
            # Exhausted means we tried to spend the target and ran out of depth.
            assert total_depth < target + 1e-6
        else:
            # Not exhausted -> consumed is within tolerance of target.
            assert abs(r.consumed_notional_usd - target) < 1e-6


# ---------------------------------------------------------------------------
# align_series
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", _seeds())
def test_align_series_is_monotone_in_time(seed: int) -> None:
    rng = random.Random(seed)
    n = 50
    base_s = 1_800_000_000
    pm = sorted([(base_s + rng.randint(0, 30 * 86400), rng.uniform(0, 1)) for _ in range(n)])
    hl = sorted(
        [(base_s * 1000 + rng.randint(0, 30 * 86400 * 1000), rng.uniform(50, 200))
         for _ in range(n)]
    )
    rows = align_series(pm, hl, bucket_minutes=720)
    times = [r.t_ms for r in rows]
    assert times == sorted(times)
    # All bucket starts are 12h grid-aligned.
    for t in times:
        assert t % BUCKET_MS == 0


# ---------------------------------------------------------------------------
# persistence_stats / forward_test invariants
# ---------------------------------------------------------------------------


def _random_trajectories(rng: random.Random, n: int) -> list[Trajectory]:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    out: list[Trajectory] = []
    for i in range(n):
        gap = rng.uniform(-0.05, 0.05)
        out.append(
            Trajectory(
                event_id=f"E{i % 5}",
                snapshot_at=base + timedelta(seconds=60 * i),
                best_gap=gap,
                bid_gap=gap,
                ask_gap=gap,
                direction="sell_yes" if gap > 0 else "buy_yes",
            )
        )
    return out


@pytest.mark.parametrize("seed", _seeds())
def test_persistence_stats_invariant_under_time_shift(seed: int) -> None:
    rng = random.Random(seed)
    traj = _random_trajectories(rng, 40)
    shifted = [
        Trajectory(
            event_id=t.event_id,
            snapshot_at=t.snapshot_at + timedelta(hours=999),
            best_gap=t.best_gap,
            bid_gap=t.bid_gap,
            ask_gap=t.ask_gap,
            direction=t.direction,
        )
        for t in traj
    ]
    p1 = persistence_stats(traj)
    p2 = persistence_stats(shifted)
    assert p1.n_snapshots == p2.n_snapshots
    assert p1.n_distinct_events == p2.n_distinct_events
    assert math.isclose(p1.gap_mean, p2.gap_mean, abs_tol=1e-12)
    assert math.isclose(p1.gap_max, p2.gap_max, abs_tol=1e-12)


@pytest.mark.parametrize("seed", _seeds())
def test_forward_test_n_entries_monotone_in_threshold(seed: int) -> None:
    """Higher threshold can only produce fewer or equal entries."""
    rng = random.Random(seed)
    traj = _random_trajectories(rng, 60)
    thresholds = [0.0, 0.005, 0.01, 0.02, 0.05]
    entries = [
        forward_test(traj, threshold=th, hold_seconds=60.0).n_entries
        for th in thresholds
    ]
    for a, b in itertools.pairwise(entries):
        assert a >= b, f"forward_test entries not monotone: {entries}"


# ---------------------------------------------------------------------------
# Empty-input edge cases — these belong with the properties since they're
# universal contracts, not specific scenarios.
# ---------------------------------------------------------------------------


def test_persistence_stats_empty_returns_zero() -> None:
    s = persistence_stats([])
    assert s.n_snapshots == 0
    assert s.gap_max == 0.0


def test_forward_test_empty_returns_zero() -> None:
    ft = forward_test([])
    assert ft.n_entries == 0
    assert ft.mean_realized_gap_at_close == 0.0
