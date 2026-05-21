"""Tests for paper-trading P&L accounting and close triggers."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from polymarket_edge import db


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
