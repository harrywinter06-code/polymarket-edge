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


def test_dashboard_renders_microstructure_section(
    tmp_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """When microstructure_classifications rows exist, the dashboard surfaces
    per-category trap-rate aggregates from the most recent scan."""
    classified_at = datetime.now(UTC).isoformat()
    rows = [
        ("scan-1", "E1", "ev1", "Ev1", "Sports", 3, "sell_yes", 0.05, 0.04, 0.03, "real"),
        ("scan-1", "E2", "ev2", "Ev2", "Sports", 3, "sell_yes", 0.05, -0.02, -0.05, "trap"),
        ("scan-1", "E3", "ev3", "Ev3", "Politics", 2, "sell_yes", 0.05, -0.10, -0.20, "trap"),
        ("scan-1", "E4", "ev4", "Ev4", "Politics", 2, "buy_yes", 0.04, 0.02, 0.001, "marginal"),
    ]
    for scan, ev, slug, title, cat, n, dirn, top_g, gs, gm, verdict in rows:
        tmp_conn.execute(
            """INSERT INTO microstructure_classifications
               (scan_id, event_id, event_slug, event_title, category_tag,
                n_markets, neg_risk_augmented, direction, top_of_book_gap,
                gap_at_small_size, gap_at_med_size, throttle_notional_usd,
                verdict, classified_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, 100.0, ?, ?)""",
            (scan, ev, slug, title, cat, n, dirn, top_g, gs, gm, verdict, classified_at),
        )
    tmp_conn.commit()
    out = tmp_path / "dashboard.html"
    write_dashboard(tmp_conn, out)
    text = out.read_text(encoding="utf-8")
    assert "live microstructure scan" in text
    assert "Sports" in text and "Politics" in text
    assert "trap rate" in text.lower()
    assert "scan-1" in text


def test_dashboard_microstructure_section_falls_back_when_empty(
    tmp_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    out = tmp_path / "dashboard.html"
    write_dashboard(tmp_conn, out)
    text = out.read_text(encoding="utf-8")
    assert "No microstructure-scan rows yet" in text


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
