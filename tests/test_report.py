"""Tests for the markdown research-note generator."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from polymarket_edge import report


def _seed_for_report(conn: sqlite3.Connection) -> None:
    """Seed the minimum rows needed for every section of generate_markdown
    to render with non-empty content."""
    fetched_at = "2026-01-01T00:00:00+00:00"
    # Two events, one flagged.
    conn.execute(
        """INSERT INTO events
           (id, slug, title, neg_risk, neg_risk_augmented, end_date,
            volume, liquidity, n_markets, fetched_at)
           VALUES ('E1', 'wide', 'Wide-gap event', 1, 0, NULL, 100.0, 100.0, 3, ?)""",
        (fetched_at,),
    )
    conn.execute(
        """INSERT INTO events
           (id, slug, title, neg_risk, neg_risk_augmented, end_date,
            volume, liquidity, n_markets, fetched_at)
           VALUES ('E2', 'narrow', 'Narrow', 1, 0, NULL, 50.0, 50.0, 2, ?)""",
        (fetched_at,),
    )
    # Arb signals: one above 2%, one below 50bp.
    for ev_id, bg, ag, dirn in (("E1", 0.030, 0.005, "sell_yes"),
                                 ("E2", 0.002, 0.003, "buy_yes")):
        conn.execute(
            """INSERT INTO event_arb_signals
               (event_id, n_markets, sum_best_bid, sum_best_ask, bid_gap, ask_gap,
                direction, has_neg_risk_other, detected_at)
               VALUES (?, 2, ?, ?, ?, ?, ?, 0, ?)""",
            (ev_id, 1.0 + bg, 1.0 - ag, bg, ag, dirn, fetched_at),
        )
    # Trajectory rows for the persistence section.
    for i in range(6):
        conn.execute(
            """INSERT INTO signal_trajectories
               (poll_run_id, event_id, n_markets, sum_best_bid, sum_best_ask,
                bid_gap, ask_gap, best_gap, direction, snapshot_at)
               VALUES ('run-a', 'E1', 3, ?, ?, ?, ?, ?, 'sell_yes', ?)""",
            (1.03 - 0.001 * i, 0.99, 0.03 - 0.001 * i, 0.01, 0.03 - 0.001 * i,
             f"2026-01-01T00:0{i}:00+00:00"),
        )
    # Hyperliquid history across multiple coins, enough rows for the backtest.
    for coin in ("BTC", "ETH", "SOL", "ARB", "OP", "DOGE", "LINK"):
        for i in range(60):
            conn.execute(
                """INSERT INTO hl_funding_history (coin, t, funding, premium, fetched_at)
                   VALUES (?, ?, ?, NULL, ?)""",
                (coin, i * 3_600_000, 0.0001 + 0.00001 * (hash(coin) % 5), fetched_at),
            )
    # A closed paper position so the paper section renders.
    conn.execute(
        """INSERT INTO paper_positions
           (venue, event_id, side, notional_usd, entry_gap, opened_at,
            closed_at, realized_pnl_usd, close_reason)
           VALUES ('polymarket', 'E1', 'sell_yes', 100.0, 0.03,
                   '2026-01-01T00:00:00+00:00', '2026-01-01T01:00:00+00:00',
                   1.5, 'decay')"""
    )
    conn.commit()


def test_write_report_produces_markdown(
    tmp_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _seed_for_report(tmp_conn)
    out = tmp_path / "REPORT.md"
    result = report.write_report(tmp_conn, out)
    assert result == out
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    # Headline section markers.
    assert "# polymarket-edge — research note" in text
    assert "Polymarket — event-level no-arb signals" in text
    assert "Hyperliquid — funding-capture backtest" in text
    assert "Live paper-trading" in text
    # Headline numbers from our seed should appear.
    assert "Wide-gap event" in text
    # Persistence section should not be the empty placeholder.
    assert "no monitor runs recorded" not in text.lower()


def test_write_report_handles_empty_db(
    tmp_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """An empty DB still writes a report with placeholder sections."""
    out = tmp_path / "REPORT.md"
    report.write_report(tmp_conn, out)
    text = out.read_text(encoding="utf-8")
    assert "# polymarket-edge — research note" in text
    # Persistence falls back to the empty placeholder.
    assert "No monitor runs recorded" in text


def test_generate_markdown_includes_threshold_table(
    tmp_conn: sqlite3.Connection,
) -> None:
    _seed_for_report(tmp_conn)
    md = report.generate_markdown(tmp_conn)
    assert "| threshold | n distinct events |" in md
    assert "0.0050" in md or "0.005" in md


def test_persistence_section_handles_no_runs(tmp_conn: sqlite3.Connection) -> None:
    md, n = report._persistence_section(tmp_conn)
    assert n == 0
    assert "No monitor runs recorded" in md


def test_persistence_section_uses_longest_run(tmp_conn: sqlite3.Connection) -> None:
    _seed_for_report(tmp_conn)
    md, n = report._persistence_section(tmp_conn)
    assert n == 6
    assert "Observation window" in md


def test_paper_section_empty_falls_back(tmp_conn: sqlite3.Connection) -> None:
    md = report._paper_section(tmp_conn)
    assert "No paper-trading rounds" in md


def test_hl_backtest_section_empty_falls_back(tmp_conn: sqlite3.Connection) -> None:
    md = report._hl_backtest_section(tmp_conn)
    assert "No Hyperliquid funding history present" in md
