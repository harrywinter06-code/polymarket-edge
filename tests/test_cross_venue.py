"""Tests for cross_venue.align_series and cross_venue.compute_lead_lag."""

from __future__ import annotations

import math
import random

from polymarket_edge.cross_venue import (
    AlignedRow,
    align_series,
    compute_lead_lag,
)

BUCKET_MS = 12 * 3_600_000  # 12 hours


def test_align_series_buckets_correctly() -> None:
    """Two PM points and three HL points spanning two 12h buckets should
    align to two rows; within-bucket the *last* observation wins, dropped
    buckets that lack one leg do not appear, and the per-bucket delta is the
    difference between consecutive surviving buckets."""
    # Anchor at 2026-01-01 00:00:00 UTC = 1767225600 s = 1767225600000 ms.
    # Bucket A start = 1767225600000, Bucket B start = A + 12h.
    a_start = 1767225600000
    b_start = a_start + BUCKET_MS

    # PM input is (unix_seconds, price). Two points in A (last wins -> 0.55),
    # two in B (last wins -> 0.60).
    pm = [
        ((a_start + 1_000_000) // 1000, 0.50),
        ((a_start + 11 * 3_600_000) // 1000, 0.55),
        ((b_start + 1_000_000) // 1000, 0.58),
        ((b_start + 11 * 3_600_000) // 1000, 0.60),
    ]
    # HL input is (unix_ms, mark). Three points in A (last -> 100.0),
    # two in B (last -> 110.0).
    hl = [
        (a_start + 500_000, 95.0),
        (a_start + 5 * 3_600_000, 98.0),
        (a_start + 11 * 3_600_000 + 500_000, 100.0),
        (b_start + 500_000, 105.0),
        (b_start + 11 * 3_600_000, 110.0),
    ]

    rows = align_series(pm, hl, bucket_minutes=720)

    assert len(rows) == 2
    assert rows[0].t_ms == a_start
    assert rows[1].t_ms == b_start
    assert rows[0].pm_price == 0.55
    assert rows[1].pm_price == 0.60
    assert rows[0].hl_mark == 100.0
    assert rows[1].hl_mark == 110.0
    # First bucket is seeded with zero deltas (no prior).
    assert rows[0].pm_delta == 0.0
    assert rows[0].hl_log_return == 0.0
    # Second bucket: delta = 0.60 - 0.55, log_return = log(110/100).
    assert math.isclose(rows[1].pm_delta, 0.05, abs_tol=1e-12)
    assert math.isclose(rows[1].hl_log_return, math.log(110.0 / 100.0), abs_tol=1e-12)


def test_compute_lead_lag_handles_perfect_correlation() -> None:
    """If HL log-returns are the PM deltas shifted forward by exactly 1 bucket,
    the maximum correlation should land at lag=+1 (PM leads HL by 1) with r=1.0,
    and lag=-1 should NOT be the maximum."""
    # Build 30 aligned rows; pm_delta is a deterministic varied sequence,
    # hl_log_return at row i equals pm_delta at row i-1 (i.e. PM leads by 1).
    n = 30
    pm_deltas = [math.sin(i * 0.4) + 0.001 * i for i in range(n)]
    # Shift: hl_log_return[i] = pm_delta[i-1]; row 0 gets 0.0.
    hl_returns = [0.0, *pm_deltas[:-1]]

    rows: list[AlignedRow] = [
        AlignedRow(
            t_ms=1_000_000_000_000 + i * BUCKET_MS,
            pm_price=0.5,        # unused by compute_lead_lag
            hl_mark=100.0,       # unused by compute_lead_lag
            pm_delta=pm_deltas[i],
            hl_log_return=hl_returns[i],
        )
        for i in range(n)
    ]

    lags = compute_lead_lag(rows, max_lag_buckets=4)

    # The first row is stripped inside compute_lead_lag (its deltas are seeds).
    # After stripping, pm_delta[k] (k>=0) corresponds to original pm_deltas[k+1],
    # and hl_log_return[k] corresponds to original hl_returns[k+1] = pm_deltas[k].
    # So hl_log_return[k] == pm_delta[k-1], which is the +1-lag relationship.
    # corr(pm[: N-1], hl[1:]) at lag=+1 should be 1.0.
    finite = {k: v for k, v in lags.items() if not math.isnan(v)}
    best_lag = max(finite, key=lambda k: finite[k])
    assert best_lag == 1, f"expected best lag at +1, got {best_lag}; lags={finite}"
    assert math.isclose(finite[1], 1.0, abs_tol=1e-9)
    # The -1 lag should be markedly worse (not 1.0).
    assert finite.get(-1, 0.0) < 0.99


def test_compute_lead_lag_returns_zero_for_independent() -> None:
    """Two independent random series with fixed seed should have max |r| under
    a loose bound. 100 rows of N(0,1) draws have a standard error on r of about
    1/sqrt(100) = 0.1; we check we stay under 0.35 across nine candidate lags."""
    rng = random.Random(20260521)
    n = 100
    rows: list[AlignedRow] = [
        AlignedRow(
            t_ms=1_000_000_000_000 + i * BUCKET_MS,
            pm_price=0.5,
            hl_mark=100.0,
            pm_delta=rng.gauss(0.0, 1.0),
            hl_log_return=rng.gauss(0.0, 1.0),
        )
        for i in range(n)
    ]
    lags = compute_lead_lag(rows, max_lag_buckets=4)
    finite = [v for v in lags.values() if not math.isnan(v)]
    assert finite, "expected at least one finite correlation"
    assert max(abs(v) for v in finite) < 0.35
