"""Tests for the single-file HTML dashboard generator.

The dashboard's contract is portability: it must open identically when emailed,
hosted on a static URL, or attached to a job application. These tests enforce
the two invariants that protect that contract — self-containment (no external
asset references) and graceful degradation when charts haven't been rendered.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from polymarket_edge import db
from polymarket_edge.dashboard import write_dashboard

_EXTERNAL_TAG = re.compile(
    r"<(?:link|script)\b[^>]*\b(?:href|src)\s*=\s*[\"']https?://",
    re.IGNORECASE,
)


@pytest.fixture
def tmp_conn(tmp_path: Path) -> sqlite3.Connection:
    p = tmp_path / "test.db"
    conn = db.connect(p)
    db.init_schema(conn)
    return conn


def _seed_minimal(conn: sqlite3.Connection) -> None:
    """Seed one event + one arb signal so the flagged-events table renders rows."""
    fetched_at = datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT INTO events (id, slug, title, neg_risk, neg_risk_augmented, end_date,
                                volume, liquidity, n_markets, fetched_at)
           VALUES (?, ?, ?, 1, 0, NULL, 1000.0, 1000.0, 2, ?)""",
        ("evt-1", "test-event", "Test negRisk event", fetched_at),
    )
    conn.execute(
        """INSERT INTO event_arb_signals (event_id, n_markets, sum_best_bid, sum_best_ask,
                                          bid_gap, ask_gap, direction, has_neg_risk_other,
                                          detected_at)
           VALUES (?, 2, 1.015, 1.0, 0.015, 0.0, 'sell_yes', 0, ?)""",
        ("evt-1", fetched_at),
    )
    # A couple of funding ticks so the HL strategy table renders without an
    # empty-fallback message (the backtest needs at least trailing+rebal hours).
    for coin in ("BTC", "ETH", "SOL", "ARB", "OP", "DOGE"):
        for i in range(40):
            conn.execute(
                """INSERT INTO hl_funding_history (coin, t, funding, premium, fetched_at)
                   VALUES (?, ?, ?, NULL, ?)""",
                (coin, i * 3_600_000, 0.0001, fetched_at),
            )
    conn.commit()


def test_dashboard_writes_self_contained_html(
    tmp_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _seed_minimal(tmp_conn)
    out = tmp_path / "dashboard.html"
    result = write_dashboard(tmp_conn, out)
    assert result == out
    assert out.exists()
    size = out.stat().st_size
    assert size > 5 * 1024, f"dashboard.html only {size} bytes — expected > 5KB"
    text = out.read_text(encoding="utf-8")
    assert "<html" in text
    assert "</html>" in text
    # No external CDN, font, or script references — the file must be portable.
    assert _EXTERNAL_TAG.search(text) is None, (
        "dashboard.html references an external http(s) asset via <link> or <script>"
    )


def test_dashboard_handles_missing_pngs(
    tmp_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    # Empty DB, no PNGs in tmp_path — both charts should fall back to the placeholder.
    assert not list(tmp_path.glob("*.png"))
    out = tmp_path / "dashboard.html"
    result = write_dashboard(tmp_conn, out)
    assert result == out
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "Chart not yet rendered" in text
    # And still self-contained.
    assert _EXTERNAL_TAG.search(text) is None
