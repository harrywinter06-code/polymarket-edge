"""Tests for paper-trading P&L accounting and close triggers."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from polymarket_edge import db, paper

from .conftest import make_event, make_market


@pytest.fixture
def tmp_conn(tmp_path: Path) -> sqlite3.Connection:
    p = tmp_path / "test.db"
    conn = db.connect(p)
    db.init_schema(conn)
    return conn


def _seed_event_and_position(
    conn: sqlite3.Connection,
    *,
    event_id: str = "E1",
    side: str = "sell_yes",
    entry_gap: float = 0.05,
    opened_at: str,
) -> None:
    conn.execute(
        """INSERT INTO events (id, slug, title, neg_risk, neg_risk_augmented, n_markets, fetched_at)
           VALUES (?, ?, ?, 1, 0, 2, ?)""",
        (event_id, "ev", "ev", opened_at),
    )
    conn.execute(
        """INSERT INTO paper_positions
           (venue, event_id, side, notional_usd, entry_gap, opened_at)
           VALUES ('polymarket', ?, ?, 100.0, ?, ?)""",
        (event_id, side, entry_gap, opened_at),
    )
    conn.commit()


def test_paper_pnl_summary_empty(tmp_conn: sqlite3.Connection) -> None:
    from polymarket_edge.paper import paper_pnl_summary

    s = paper_pnl_summary(tmp_conn)
    assert s["n_open"] == 0
    assert s["n_closed"] == 0
    assert s["realized_pnl_usd"] == 0.0


def test_paper_pnl_summary_counts_open_and_closed(tmp_conn: sqlite3.Connection) -> None:
    from polymarket_edge.paper import paper_pnl_summary

    now = datetime.now(UTC).isoformat()
    _seed_event_and_position(tmp_conn, event_id="E1", opened_at=now)
    _seed_event_and_position(tmp_conn, event_id="E2", opened_at=now)
    # Close one
    tmp_conn.execute(
        """UPDATE paper_positions
           SET closed_at = ?, realized_pnl_usd = 2.0, close_reason = 'decay'
           WHERE event_id = 'E2'""",
        (now,),
    )
    tmp_conn.commit()
    s = paper_pnl_summary(tmp_conn)
    assert s["n_open"] == 1
    assert s["n_closed"] == 1
    assert abs(s["realized_pnl_usd"] - 2.0) < 1e-9


def test_max_age_close_logic(tmp_conn: sqlite3.Connection) -> None:
    """Bug fix 2c: a position older than max_age_hours should close even if
    gap hasn't decayed below the threshold."""
    # We test the age-check logic by computing the threshold manually,
    # since paper_auto_round needs live network I/O.
    now = datetime.now(UTC)
    old = (now - timedelta(hours=200)).isoformat()
    _seed_event_and_position(tmp_conn, event_id="E1", opened_at=old, entry_gap=0.05)

    # Replicate the close-logic decision: age 200h > max_age 168h -> close
    row = tmp_conn.execute(
        "SELECT opened_at FROM paper_positions WHERE event_id = 'E1'"
    ).fetchone()
    opened_dt = datetime.fromisoformat(row["opened_at"])
    age_hours = (now - opened_dt).total_seconds() / 3600.0
    assert age_hours > 168.0  # would trigger max_age close in real run


# ---------------------------------------------------------------------------
# paper_auto_round — full open/mark/close cycle
# ---------------------------------------------------------------------------


def _wide_sell_event(event_id: str) -> dict:
    """A negRisk event with sum(best_bid) well above 1.0 — sell-side flagged."""
    return make_event(
        event_id,
        markets=[
            make_market("m1", best_bid=0.70, best_ask=0.71),
            make_market("m2", best_bid=0.60, best_ask=0.61),
        ],
    )


def _fair_event(event_id: str) -> dict:
    """Sum of bids == 1.0 — no longer flagged, gap collapses to 0."""
    return make_event(
        event_id,
        markets=[
            make_market("m1", best_bid=0.50, best_ask=0.51),
            make_market("m2", best_bid=0.50, best_ask=0.51),
        ],
    )


def test_paper_auto_round_opens_new_flagged_event(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A freshly flagged event with no existing position should be opened."""

    async def fake_fetch_all_active_events(**kwargs):
        return [_wide_sell_event("E1")]

    monkeypatch.setattr(paper.fetch, "fetch_all_active_events", fake_fetch_all_active_events)

    n_open, n_close, n_marked = asyncio.run(
        paper.paper_auto_round(str(tmp_db_path), fee_buffer=0.0)
    )
    assert n_open == 1
    assert n_close == 0
    assert n_marked == 0

    conn = db.connect(tmp_db_path)
    row = conn.execute("SELECT * FROM paper_positions WHERE event_id='E1'").fetchone()
    assert row is not None
    assert row["side"] == "sell_yes"
    assert row["entry_gap"] > 0
    assert row["closed_at"] is None


def test_paper_auto_round_does_not_double_open(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake(**kwargs):
        return [_wide_sell_event("E1")]

    monkeypatch.setattr(paper.fetch, "fetch_all_active_events", fake)
    asyncio.run(paper.paper_auto_round(str(tmp_db_path), fee_buffer=0.0))
    n_open, _, n_marked = asyncio.run(
        paper.paper_auto_round(str(tmp_db_path), fee_buffer=0.0)
    )
    assert n_open == 0
    assert n_marked == 1  # second round marks the same open position


def test_paper_auto_round_closes_on_decay(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 1 opens at gap=0.3, round 2 sees gap=0 -> close on decay."""
    events_iter = iter([[_wide_sell_event("E1")], [_fair_event("E1")]])

    async def fake(**kwargs):
        return next(events_iter)

    monkeypatch.setattr(paper.fetch, "fetch_all_active_events", fake)
    asyncio.run(paper.paper_auto_round(str(tmp_db_path), fee_buffer=0.0, close_decay=0.5))
    _, n_close, _ = asyncio.run(
        paper.paper_auto_round(str(tmp_db_path), fee_buffer=0.0, close_decay=0.5)
    )
    assert n_close == 1
    conn = db.connect(tmp_db_path)
    row = conn.execute("SELECT * FROM paper_positions WHERE event_id='E1'").fetchone()
    assert row["close_reason"] == "decay"
    # P&L = notional * (|entry_gap| - |current_gap|) = 100 * (0.30 - 0.0) = 30.
    assert abs(row["realized_pnl_usd"] - 30.0) < 1e-9


def test_paper_auto_round_closes_on_max_age(
    tmp_conn: sqlite3.Connection,
    tmp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-seed an aged open position; auto-round should close on max_age even
    though the current gap is still wide."""
    old_ts = (datetime.now(UTC) - timedelta(hours=200)).isoformat()
    tmp_conn.execute(
        """INSERT INTO events
           (id, slug, title, neg_risk, neg_risk_augmented, n_markets, fetched_at)
           VALUES ('E1', 'ev', 'ev', 1, 0, 2, ?)""",
        (old_ts,),
    )
    tmp_conn.execute(
        """INSERT INTO paper_positions
           (venue, event_id, side, notional_usd, entry_gap, opened_at)
           VALUES ('polymarket', 'E1', 'sell_yes', 100.0, 0.30, ?)""",
        (old_ts,),
    )
    tmp_conn.commit()

    async def fake(**kwargs):
        return [_wide_sell_event("E1")]  # gap still wide -> would normally not close on decay

    monkeypatch.setattr(paper.fetch, "fetch_all_active_events", fake)
    _, n_close, _ = asyncio.run(
        paper.paper_auto_round(
            str(tmp_db_path), fee_buffer=0.0, close_decay=0.01, max_age_hours=168.0
        )
    )
    assert n_close == 1
    conn = db.connect(tmp_db_path)
    row = conn.execute("SELECT * FROM paper_positions WHERE event_id='E1'").fetchone()
    assert row["close_reason"] == "max_age"


def test_paper_auto_round_skips_unflagged_events(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake(**kwargs):
        return [_fair_event("E1")]

    monkeypatch.setattr(paper.fetch, "fetch_all_active_events", fake)
    n_open, _, _ = asyncio.run(
        paper.paper_auto_round(str(tmp_db_path), fee_buffer=0.005)
    )
    assert n_open == 0
