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

from polymarket_edge import book_depth, db, detector, fetch


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
    capture_books_for_flagged: bool = False,
    book_capture_fee_buffer: float = 0.005,
) -> tuple[str, int, int]:
    """Run the poll loop.

    Returns (poll_run_id, n_polls, n_trajectories_written).

    When ``capture_books_for_flagged`` is True, for every event whose
    detector best_gap exceeds ``book_capture_fee_buffer`` we also fetch
    the full /book for each constituent YES token and persist a
    `book_snapshots` row. This is the forward-only data feed used to
    replace the noisy 5-minute-delta half-spread proxy in the MM simulator.
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
            # Upsert event so the trajectory FK resolves even for events that
            # appeared after the last `ingest` run.
            db.upsert_event(conn, ev, snapshot_at)
            _insert_trajectory(conn, poll_run_id=poll_run_id, sig=sig, snapshot_at=snapshot_at)
            n_written += 1
            if capture_books_for_flagged and sig.best_gap >= book_capture_fee_buffer:
                await _capture_books_for_event(
                    conn, ev, snapshot_at=snapshot_at
                )
        conn.commit()
        n_polls += 1

        remaining = end_at - datetime.now(UTC).timestamp()
        if remaining <= 0:
            break
        await asyncio.sleep(min(poll_interval_seconds, remaining))

    return poll_run_id, n_polls, n_written


async def _capture_books_for_event(
    conn: sqlite3.Connection,
    event: dict,
    *,
    snapshot_at: str,
) -> None:
    """Fetch and persist book snapshots for every active market in an event."""
    markets = [
        m for m in event.get("markets", [])
        if m.get("active") and not m.get("closed") and m.get("acceptingOrders")
    ]
    if not markets:
        return
    try:
        books = await book_depth.fetch_books_for_event(markets)
    except Exception:  # network boundary; skip this event for this poll
        return
    event_id = str(event["id"])
    for m in markets:
        market_id = str(m.get("id")) if m.get("id") is not None else None
        # The yes-token-id is the key book_depth.fetch_books_for_event uses.
        # Recover it from clobTokenIds[0] the same way that function does.
        raw = m.get("clobTokenIds")
        if not raw:
            continue
        try:
            import json as _json
            tokens = _json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            continue
        if not tokens:
            continue
        yes_id = str(tokens[0])
        book = books.get(yes_id)
        if book is None:
            continue
        db.insert_book_snapshot(
            conn,
            token_id=yes_id,
            market_id=market_id,
            event_id=event_id,
            snapshot_at=snapshot_at,
            bids=[(lvl.price, lvl.size) for lvl in book.bids],
            asks=[(lvl.price, lvl.size) for lvl in book.asks],
        )


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
