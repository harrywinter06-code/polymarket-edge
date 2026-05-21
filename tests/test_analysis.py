"""Tests for persistence/forward-test analysis over signal trajectories."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from polymarket_edge.analysis import (
    Trajectory,
    forward_test,
    persistence_stats,
    threshold_counts,
)


def _t(event_id: str, t_offset_s: float, gap: float, direction: str = "sell_yes") -> Trajectory:
    base = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)
    return Trajectory(
        event_id=event_id,
        snapshot_at=base + timedelta(seconds=t_offset_s),
        best_gap=gap,
        bid_gap=gap if direction == "sell_yes" else 0.0,
        ask_gap=gap if direction == "buy_yes" else 0.0,
        direction=direction,
    )


def test_persistence_stats_empty() -> None:
    ps = persistence_stats([])
    assert ps.n_snapshots == 0
    assert ps.gap_max == 0.0


def test_persistence_stats_basic() -> None:
    traj = [
        _t("e1", 0, 0.01),
        _t("e1", 60, 0.02),
        _t("e2", 0, 0.05),
        _t("e2", 60, 0.03),
    ]
    ps = persistence_stats(traj)
    assert ps.n_snapshots == 4
    assert ps.n_distinct_events == 2
    assert ps.gap_max == 0.05
    # mean = (0.01 + 0.02 + 0.05 + 0.03) / 4 = 0.0275
    assert abs(ps.gap_mean - 0.0275) < 1e-9


def test_threshold_counts_dedupes_per_event() -> None:
    traj = [
        _t("e1", 0, 0.001),
        _t("e1", 60, 0.06),
        _t("e1", 120, 0.04),
        _t("e2", 0, 0.03),
        _t("e3", 0, 0.0001),
    ]
    counts = {tc.threshold: tc.n_events_ever_crossed for tc in threshold_counts(traj)}
    assert counts[0.005] == 2  # e1, e2
    assert counts[0.01] == 2
    assert counts[0.02] == 2
    assert counts[0.05] == 1  # only e1


def test_forward_test_measures_decay() -> None:
    # e1: gap 5% at t=0, decays to 1% at t=600s (decay 4%)
    # e2: gap 3% at t=0, decays to 2% at t=600s (decay 1%)
    # e3: gap 0.1% at t=0 -> below threshold, not counted
    traj = [
        _t("e1", 0, 0.05),
        _t("e1", 600, 0.01),
        _t("e2", 0, 0.03),
        _t("e2", 600, 0.02),
        _t("e3", 0, 0.001),
        _t("e3", 600, 0.001),
    ]
    ft = forward_test(traj, threshold=0.005, hold_seconds=300)
    assert ft.n_entries == 2
    # mean realized = (0.01 + 0.02) / 2 = 0.015
    assert abs(ft.mean_realized_gap_at_close - 0.015) < 1e-9
    # mean decay = ((0.05 - 0.01) + (0.03 - 0.02)) / 2 = 0.025
    assert abs(ft.mean_gap_decay - 0.025) < 1e-9


def test_forward_test_skips_entries_with_no_close_after_hold() -> None:
    # entry at t=0 with gap 5%, but only sample available before hold elapses
    traj = [
        _t("e1", 0, 0.05),
        _t("e1", 60, 0.04),  # only 60s later, hold requires 300s
    ]
    ft = forward_test(traj, threshold=0.005, hold_seconds=300)
    assert ft.n_entries == 0
