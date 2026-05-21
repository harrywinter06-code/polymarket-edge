"""Persistence / forward-test analysis over saved signal trajectories.

For a given observation window (poll_run_id), answers:
- Distribution of |best_gap| at each snapshot (mean, max, quantiles).
- Number of distinct events that ever crossed thresholds (50/100/200bp).
- For each "candidate entry" (gap exceeded threshold), the realized gap K
  minutes later — proxy for naive arb hold-to-revert P&L.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Trajectory:
    event_id: str
    snapshot_at: datetime
    best_gap: float
    bid_gap: float
    ask_gap: float
    direction: str


@dataclass(frozen=True, slots=True)
class PersistenceStats:
    n_snapshots: int
    n_distinct_events: int
    gap_mean: float
    gap_p50: float
    gap_p90: float
    gap_p99: float
    gap_max: float


@dataclass(frozen=True, slots=True)
class ThresholdCount:
    threshold: float
    n_events_ever_crossed: int


@dataclass(frozen=True, slots=True)
class ForwardTest:
    threshold: float
    hold_seconds: float
    n_entries: int
    mean_realized_gap_at_close: float
    mean_gap_decay: float  # entry_gap - close_gap, positive = reverted toward 0


def _parse_iso(s: str) -> datetime:
    # fromisoformat handles tz-aware ISO strings on Python 3.11+
    return datetime.fromisoformat(s)


def to_trajectories(rows: Iterable[object]) -> list[Trajectory]:
    """Coerce sqlite3.Row (or dict-like) iterable into Trajectory list."""
    out: list[Trajectory] = []
    for r in rows:
        # Row objects support dict-style indexing
        out.append(
            Trajectory(
                event_id=r["event_id"],
                snapshot_at=_parse_iso(r["snapshot_at"]),
                best_gap=float(r["best_gap"]),
                bid_gap=float(r["bid_gap"]),
                ask_gap=float(r["ask_gap"]),
                direction=r["direction"],
            )
        )
    return out


def persistence_stats(traj: Sequence[Trajectory]) -> PersistenceStats:
    if not traj:
        return PersistenceStats(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0)
    abs_gaps = [abs(t.best_gap) for t in traj]
    quantile = (lambda xs, q: statistics.quantiles(xs, n=100)[q - 1]) if len(traj) >= 2 else (
        lambda xs, q: xs[0]
    )
    return PersistenceStats(
        n_snapshots=len(traj),
        n_distinct_events=len({t.event_id for t in traj}),
        gap_mean=statistics.fmean(abs_gaps),
        gap_p50=quantile(sorted(abs_gaps), 50),
        gap_p90=quantile(sorted(abs_gaps), 90),
        gap_p99=quantile(sorted(abs_gaps), 99),
        gap_max=max(abs_gaps),
    )


def threshold_counts(
    traj: Sequence[Trajectory],
    thresholds: Sequence[float] = (0.005, 0.01, 0.02, 0.05),
) -> list[ThresholdCount]:
    out: list[ThresholdCount] = []
    for th in thresholds:
        events_ever = {t.event_id for t in traj if abs(t.best_gap) >= th}
        out.append(ThresholdCount(threshold=th, n_events_ever_crossed=len(events_ever)))
    return out


def _per_event(traj: Sequence[Trajectory]) -> dict[str, list[Trajectory]]:
    out: dict[str, list[Trajectory]] = {}
    for t in traj:
        out.setdefault(t.event_id, []).append(t)
    for k in out:
        out[k].sort(key=lambda x: x.snapshot_at)
    return out


def forward_test(
    traj: Sequence[Trajectory],
    *,
    threshold: float = 0.005,
    hold_seconds: float = 300.0,
) -> ForwardTest:
    """For each event-snapshot where |best_gap| >= threshold, find the next
    snapshot for that event that is at least hold_seconds later, and record
    how much the gap decayed (positive = reverted toward fair).
    """
    per_event = _per_event(traj)
    realized: list[float] = []
    decays: list[float] = []
    for series in per_event.values():
        for i, entry in enumerate(series):
            if abs(entry.best_gap) < threshold:
                continue
            # find a snapshot at least hold_seconds later
            target_close = None
            for j in range(i + 1, len(series)):
                dt = (series[j].snapshot_at - entry.snapshot_at).total_seconds()
                if dt >= hold_seconds:
                    target_close = series[j]
                    break
            if target_close is None:
                continue
            realized.append(target_close.best_gap)
            decays.append(abs(entry.best_gap) - abs(target_close.best_gap))
    return ForwardTest(
        threshold=threshold,
        hold_seconds=hold_seconds,
        n_entries=len(realized),
        mean_realized_gap_at_close=statistics.fmean(realized) if realized else 0.0,
        mean_gap_decay=statistics.fmean(decays) if decays else 0.0,
    )
