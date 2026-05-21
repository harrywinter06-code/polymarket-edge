"""Paper-trading engine for Polymarket negRisk event-level signals.

A single round (`paper_auto_round`) does three things:
  1. Open a position on every currently-flagged event we don't already hold.
  2. Mark every open position against the current gap.
  3. Close positions whose gap has decayed below a configurable fraction of
     the entry gap (default 50%).

P&L model is the linear approximation:

    pnl_usd = notional_usd * (|entry_gap| - |current_gap|)

This is correct on the small-gap limit for the sell-YES-basket / buy-YES-basket
trades the detector flags, ignoring fees and execution slippage. The point is to
demonstrate the end-to-end loop (detect -> size -> hold -> exit -> attribute),
not to claim realistic net returns.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from polymarket_edge import db, detector, fetch

DEFAULT_NOTIONAL_USD = 100.0
DEFAULT_FEE_BUFFER = 0.005
DEFAULT_CLOSE_DECAY = 0.5  # close when current_gap <= decay * entry_gap


async def paper_auto_round(
    db_path: str,
    *,
    fee_buffer: float = DEFAULT_FEE_BUFFER,
    notional_usd: float = DEFAULT_NOTIONAL_USD,
    close_decay: float = DEFAULT_CLOSE_DECAY,
    max_events: int | None = 300,
) -> tuple[int, int, int]:
    """Run one mark/close/open cycle. Returns (opened, closed, marked_open)."""
    events = await fetch.fetch_all_active_events(max_events=max_events)
    by_id: dict[str, dict[str, Any]] = {str(e["id"]): e for e in events}
    conn = db.connect(db_path)
    db.init_schema(conn)
    now = fetch.now_iso()

    n_opened, n_closed, n_marked = 0, 0, 0

    # 1) close-or-mark open positions
    open_rows = conn.execute(
        "SELECT * FROM paper_positions WHERE closed_at IS NULL AND venue='polymarket'"
    ).fetchall()
    open_event_ids: set[str] = set()
    for row in open_rows:
        open_event_ids.add(row["event_id"])
        ev = by_id.get(row["event_id"])
        if ev is None:
            continue
        sig = detector.score_event(ev)
        if sig is None:
            continue
        entry_gap = float(row["entry_gap"])
        side = row["side"]
        # gap relevant to the side we entered
        current = sig.bid_gap if side == "sell_yes" else sig.ask_gap
        if abs(current) <= close_decay * abs(entry_gap):
            pnl = float(row["notional_usd"]) * (abs(entry_gap) - abs(current))
            conn.execute(
                """
                UPDATE paper_positions
                SET closed_at = ?, realized_pnl_usd = ?, close_reason = 'decay'
                WHERE id = ?
                """,
                (now, pnl, row["id"]),
            )
            n_closed += 1
        else:
            n_marked += 1

    # 2) open new positions on flagged events we don't already hold
    for ev in events:
        sig = detector.score_event(ev)
        if sig is None or not detector.is_flagged(sig, fee_buffer=fee_buffer):
            continue
        if sig.event_id in open_event_ids:
            continue
        # Ensure event row exists for downstream joins
        db.upsert_event(conn, ev, now)
        side = "sell_yes" if sig.bid_gap > sig.ask_gap else "buy_yes"
        entry_gap = sig.bid_gap if side == "sell_yes" else sig.ask_gap
        conn.execute(
            """
            INSERT INTO paper_positions
            (venue, event_id, side, notional_usd, entry_gap, opened_at)
            VALUES ('polymarket', ?, ?, ?, ?, ?)
            """,
            (sig.event_id, side, notional_usd, entry_gap, now),
        )
        n_opened += 1

    conn.commit()
    return n_opened, n_closed, n_marked


def paper_pnl_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Aggregate realized + open P&L. Returns a flat dict for the CLI."""
    row = conn.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE closed_at IS NULL)                  AS n_open,
            COUNT(*) FILTER (WHERE closed_at IS NOT NULL)              AS n_closed,
            COALESCE(SUM(realized_pnl_usd), 0.0)                       AS realized_pnl,
            COALESCE(SUM(CASE WHEN closed_at IS NULL
                              THEN notional_usd ELSE 0 END), 0.0)      AS gross_open_notional
        FROM paper_positions
        """
    ).fetchone()
    return {
        "n_open": int(row["n_open"]),
        "n_closed": int(row["n_closed"]),
        "realized_pnl_usd": float(row["realized_pnl"]),
        "gross_open_notional_usd": float(row["gross_open_notional"]),
    }
