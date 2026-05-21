"""High-frequency forward observation loop for Polymarket negRisk events.

Polls active events at a fixed cadence for a fixed duration, records every
scored signal to `signal_trajectories`, and tags each run with a UUID so the
observation window can be sliced cleanly in analysis.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from polymarket_edge import db, detector, fetch


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _insert_trajectory(
    conn: sqlite3.Connection,
    *,
    poll_run_id: str,
    sig: detector.EventArbSignal,
    snapshot_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO signal_trajectories
        (poll_run_id, event_id, n_markets, sum_best_bid, sum_best_ask,
         bid_gap, ask_gap, best_gap, direction, snapshot_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            poll_run_id,
            sig.event_id,
            sig.n_markets,
            sig.sum_best_bid,
            sig.sum_best_ask,
            sig.bid_gap,
            sig.ask_gap,
            sig.best_gap,
            sig.direction,
            snapshot_at,
        ),
    )


async def run_monitor(
    db_path: str,
    *,
    duration_minutes: float,
    poll_interval_seconds: float,
    max_events_per_poll: int | None = None,
    poll_run_id: str | None = None,
) -> tuple[str, int, int]:
    """Run the poll loop.

    Returns (poll_run_id, n_polls, n_trajectories_written).
    """
    poll_run_id = poll_run_id or str(uuid.uuid4())
    conn = db.connect(db_path)
    db.init_schema(conn)

    end_at = datetime.now(UTC).timestamp() + duration_minutes * 60
    n_polls = 0
    n_written = 0
    while datetime.now(UTC).timestamp() < end_at:
        events = await fetch.fetch_all_active_events(max_events=max_events_per_poll)
        snapshot_at = now_iso()
        for ev in events:
            sig = detector.score_event(ev)
            if sig is None:
                continue
            _insert_trajectory(conn, poll_run_id=poll_run_id, sig=sig, snapshot_at=snapshot_at)
            n_written += 1
        conn.commit()
        n_polls += 1

        remaining = end_at - datetime.now(UTC).timestamp()
        if remaining <= 0:
            break
        await asyncio.sleep(min(poll_interval_seconds, remaining))

    return poll_run_id, n_polls, n_written


def list_poll_runs(conn: sqlite3.Connection) -> list[tuple[str, int, str, str]]:
    """Return [(poll_run_id, n_rows, first_at, last_at)] for analysis."""
    rows = conn.execute(
        """
        SELECT poll_run_id,
               COUNT(*) AS n,
               MIN(snapshot_at) AS first_at,
               MAX(snapshot_at) AS last_at
        FROM signal_trajectories
        GROUP BY poll_run_id
        ORDER BY MIN(snapshot_at) DESC
        """
    ).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def fetch_trajectories(
    conn: sqlite3.Connection,
    *,
    poll_run_id: str | None = None,
    event_ids: Iterable[str] | None = None,
) -> list[sqlite3.Row]:
    where: list[str] = []
    params: list[object] = []
    if poll_run_id:
        where.append("poll_run_id = ?")
        params.append(poll_run_id)
    if event_ids is not None:
        ids = list(event_ids)
        if not ids:
            return []
        where.append(f"event_id IN ({','.join(['?'] * len(ids))})")
        params.extend(ids)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    return list(
        conn.execute(
            f"""
            SELECT * FROM signal_trajectories
            {clause}
            ORDER BY event_id, snapshot_at
            """,
            params,
        )
    )
