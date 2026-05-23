"""Tests for the forward-observation poll loop."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from polymarket_edge import fetch, monitor

from .conftest import make_event, make_market


def test_now_iso_is_parseable() -> None:
    from datetime import datetime

    parsed = datetime.fromisoformat(monitor.now_iso())
    assert parsed.tzinfo is not None


def test_run_monitor_writes_trajectories(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One short window should produce trajectories for every scored event."""
    events = [make_event("E1"), make_event("E2", slug="other")]

    async def fake_fetch_all_active_events(**kwargs):
        return events

    monkeypatch.setattr(fetch, "fetch_all_active_events", fake_fetch_all_active_events)
    monkeypatch.setattr(monitor.fetch, "fetch_all_active_events", fake_fetch_all_active_events)

    # Very short window so the loop exits after one poll.
    poll_run_id, n_polls, n_written = asyncio.run(
        monitor.run_monitor(
            str(tmp_db_path),
            duration_minutes=0.001,  # 60ms
            poll_interval_seconds=0.001,
        )
    )
    assert poll_run_id
    assert n_polls >= 1
    assert n_written >= 2  # 2 events scored per poll, at least 1 poll


def test_run_monitor_uses_provided_poll_run_id(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch_all_active_events(**kwargs):
        return [make_event("E1")]

    monkeypatch.setattr(monitor.fetch, "fetch_all_active_events", fake_fetch_all_active_events)
    run_id, _, _ = asyncio.run(
        monitor.run_monitor(
            str(tmp_db_path),
            duration_minutes=0.001,
            poll_interval_seconds=0.001,
            poll_run_id="my-run",
        )
    )
    assert run_id == "my-run"


def test_list_poll_runs_returns_each_run(tmp_conn: sqlite3.Connection) -> None:
    # Seed two runs with synthetic trajectory rows.
    for run_id, ts in (("run-a", "2026-01-01T00:00:00+00:00"),
                      ("run-b", "2026-01-02T00:00:00+00:00")):
        tmp_conn.execute(
            """INSERT INTO events
               (id, slug, title, neg_risk, neg_risk_augmented, n_markets, fetched_at)
               VALUES (?, ?, ?, 1, 0, 2, ?)""",
            (f"E-{run_id}", "ev", "ev", ts),
        )
        tmp_conn.execute(
            """INSERT INTO signal_trajectories
               (poll_run_id, event_id, n_markets, sum_best_bid, sum_best_ask,
                bid_gap, ask_gap, best_gap, direction, snapshot_at)
               VALUES (?, ?, 2, 1.05, 0.95, 0.05, 0.05, 0.05, 'both', ?)""",
            (run_id, f"E-{run_id}", ts),
        )
    tmp_conn.commit()
    runs = monitor.list_poll_runs(tmp_conn)
    assert {r[0] for r in runs} == {"run-a", "run-b"}
    # Each is reported with 1 row.
    for r in runs:
        assert r[1] == 1


def test_fetch_trajectories_filters_by_run_and_event(tmp_conn: sqlite3.Connection) -> None:
    # Seed two runs, two events each.
    rows_to_insert = [
        ("run-a", "E1", "2026-01-01T00:00:00+00:00"),
        ("run-a", "E2", "2026-01-01T00:01:00+00:00"),
        ("run-b", "E1", "2026-01-02T00:00:00+00:00"),
    ]
    for ev_id in ("E1", "E2"):
        tmp_conn.execute(
            """INSERT OR IGNORE INTO events
               (id, slug, title, neg_risk, neg_risk_augmented, n_markets, fetched_at)
               VALUES (?, ?, ?, 1, 0, 2, ?)""",
            (ev_id, "ev", "ev", "2026-01-01T00:00:00+00:00"),
        )
    for run_id, ev_id, ts in rows_to_insert:
        tmp_conn.execute(
            """INSERT INTO signal_trajectories
               (poll_run_id, event_id, n_markets, sum_best_bid, sum_best_ask,
                bid_gap, ask_gap, best_gap, direction, snapshot_at)
               VALUES (?, ?, 2, 1.05, 0.95, 0.05, 0.05, 0.05, 'both', ?)""",
            (run_id, ev_id, ts),
        )
    tmp_conn.commit()

    # All run-a rows:
    assert len(monitor.fetch_trajectories(tmp_conn, poll_run_id="run-a")) == 2
    # Just E1 across runs:
    assert len(monitor.fetch_trajectories(tmp_conn, event_ids=["E1"])) == 2
    # Run + event together:
    assert len(monitor.fetch_trajectories(tmp_conn, poll_run_id="run-b", event_ids=["E1"])) == 1
    # Empty event_ids list short-circuits to [].
    assert monitor.fetch_trajectories(tmp_conn, event_ids=[]) == []


def test_fetch_trajectories_no_filter_returns_all(tmp_conn: sqlite3.Connection) -> None:
    tmp_conn.execute(
        """INSERT INTO events
           (id, slug, title, neg_risk, neg_risk_augmented, n_markets, fetched_at)
           VALUES ('E1', 'ev', 'ev', 1, 0, 2, 'a')"""
    )
    tmp_conn.execute(
        """INSERT INTO signal_trajectories
           (poll_run_id, event_id, n_markets, sum_best_bid, sum_best_ask,
            bid_gap, ask_gap, best_gap, direction, snapshot_at)
           VALUES ('r', 'E1', 2, 1.05, 0.95, 0.05, 0.05, 0.05, 'both', 'a')"""
    )
    tmp_conn.commit()
    rows = monitor.fetch_trajectories(tmp_conn)
    assert len(rows) == 1


def test_run_monitor_captures_books_for_flagged_events(
    tmp_db_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With capture_books_for_flagged=True, the loop writes a book_snapshots
    row per active market for events whose detector best_gap clears the
    threshold. Unflagged events do NOT trigger a book fetch."""
    from polymarket_edge import book_depth
    from polymarket_edge import db as db_module
    from polymarket_edge.book_depth import Level, MarketBook

    # Wide-sell event (gap ~0.15) -> flagged.
    flagged = make_event(
        "E-FLAG",
        markets=[
            make_market("m1", best_bid=0.60, best_ask=0.61),
            make_market("m2", best_bid=0.55, best_ask=0.56),
        ],
    )
    # Fair event -> not flagged.
    fair = make_event(
        "E-FAIR",
        slug="fair",
        markets=[
            make_market("m3", best_bid=0.50, best_ask=0.51),
            make_market("m4", best_bid=0.50, best_ask=0.51),
        ],
    )

    async def fake_fetch(**kwargs):
        return [flagged, fair]

    async def fake_books(markets, **kwargs):
        out = {}
        for m in markets:
            yes = m["clobTokenIds"].strip("[]").replace('"', "").split(",")[0].strip()
            out[yes] = MarketBook(
                token_id=yes,
                bids=[Level(0.60, 100.0), Level(0.59, 50.0)],
                asks=[Level(0.61, 100.0)],
            )
        return out

    monkeypatch.setattr(monitor.fetch, "fetch_all_active_events", fake_fetch)
    monkeypatch.setattr(book_depth, "fetch_books_for_event", fake_books)
    monkeypatch.setattr(monitor.book_depth, "fetch_books_for_event", fake_books)

    asyncio.run(
        monitor.run_monitor(
            str(tmp_db_path),
            duration_minutes=0.001,
            poll_interval_seconds=0.001,
            capture_books_for_flagged=True,
            book_capture_fee_buffer=0.01,
        )
    )
    conn = db_module.connect(tmp_db_path)
    rows = conn.execute(
        "SELECT token_id, event_id, half_spread FROM book_snapshots"
    ).fetchall()
    conn.close()
    # Only the flagged event's two markets should have produced book rows;
    # the fair event has best_gap = 0 which doesn't clear the buffer. The
    # loop may run multiple times in the short test window — assert
    # cardinality of distinct (event, market) pairs rather than total rows.
    event_ids = {r["event_id"] for r in rows}
    assert event_ids == {"E-FLAG"}
    tokens = {r["token_id"] for r in rows}
    assert len(tokens) == 2  # m1 and m2 of the flagged event
    for r in rows:
        assert abs(r["half_spread"] - 0.005) < 1e-9


def test_run_monitor_book_fetch_failure_does_not_kill_loop(
    tmp_db_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A network failure on the /book fetch must not stop the monitor loop —
    the trajectory still gets written, the book just isn't captured."""
    from polymarket_edge import book_depth
    from polymarket_edge import db as db_module

    flagged = make_event(
        "E1",
        markets=[
            make_market("m1", best_bid=0.60, best_ask=0.61),
            make_market("m2", best_bid=0.55, best_ask=0.56),
        ],
    )

    async def fake_fetch(**kwargs):
        return [flagged]

    async def boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(monitor.fetch, "fetch_all_active_events", fake_fetch)
    monkeypatch.setattr(book_depth, "fetch_books_for_event", boom)
    monkeypatch.setattr(monitor.book_depth, "fetch_books_for_event", boom)

    _, _, n_written = asyncio.run(
        monitor.run_monitor(
            str(tmp_db_path),
            duration_minutes=0.001,
            poll_interval_seconds=0.001,
            capture_books_for_flagged=True,
            book_capture_fee_buffer=0.0,
        )
    )
    conn = db_module.connect(tmp_db_path)
    n_traj = conn.execute("SELECT COUNT(*) FROM signal_trajectories").fetchone()[0]
    n_books = conn.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0]
    conn.close()
    assert n_traj >= 1
    assert n_books == 0
    assert n_written >= 1
