"""CLI for polymarket-edge."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from polymarket_edge import db, detector, fetch

app = typer.Typer(add_completion=False, help="Polymarket event-level edge scanner")

DEFAULT_DB = Path("polymarket_edge.db")


@app.command()
def ingest(
    db_path: Path = DEFAULT_DB,
    max_events: int = typer.Option(0, help="Cap on events fetched (0 = no cap)"),
) -> None:
    """Pull active events + markets from gamma and persist a snapshot to SQLite."""
    cap = max_events or None
    events = asyncio.run(fetch.fetch_all_active_events(max_events=cap))
    typer.echo(f"fetched {len(events)} events")

    conn = db.connect(db_path)
    db.init_schema(conn)
    fetched_at = fetch.now_iso()
    n_markets = 0
    for ev in events:
        db.upsert_event(conn, ev, fetched_at)
        for m in ev.get("markets", []):
            db.upsert_market(conn, m, str(ev["id"]), fetched_at)
            db.insert_market_snapshot(conn, m, fetched_at)
            n_markets += 1
    conn.commit()
    typer.echo(
        f"persisted {len(events)} events, {n_markets} market snapshots -> {db_path}"
    )


@app.command()
def scan(
    db_path: Path = DEFAULT_DB,
    fee_buffer: float = typer.Option(0.02, help="Min gap to flag (covers fees)"),
    max_events: int = typer.Option(0, help="Cap on events scanned (0 = no cap)"),
    top: int = typer.Option(25, help="How many flagged events to print"),
) -> None:
    """Fetch live events, score every negRisk event, persist + print flagged ones."""
    cap = max_events or None
    events = asyncio.run(fetch.fetch_all_active_events(max_events=cap))
    conn = db.connect(db_path)
    db.init_schema(conn)
    detected_at = fetch.now_iso()

    flagged: list[detector.EventArbSignal] = []
    scored = 0
    for ev in events:
        sig = detector.score_event(ev)
        if sig is None:
            continue
        scored += 1
        db.insert_arb_signal(
            conn,
            event_id=sig.event_id,
            n_markets=sig.n_markets,
            sum_best_bid=sig.sum_best_bid,
            sum_best_ask=sig.sum_best_ask,
            bid_gap=sig.bid_gap,
            ask_gap=sig.ask_gap,
            direction=sig.direction,
            has_neg_risk_other=sig.has_neg_risk_other,
            detected_at=detected_at,
        )
        if detector.is_flagged(sig, fee_buffer=fee_buffer):
            flagged.append(sig)
    conn.commit()

    typer.echo(
        f"scored {scored} negRisk events; {len(flagged)} flagged at fee_buffer={fee_buffer}"
    )
    flagged.sort(key=lambda s: s.best_gap, reverse=True)
    for s in flagged[:top]:
        other = " [has_other]" if s.has_neg_risk_other else ""
        typer.echo(
            f"  gap={s.best_gap:+.4f} {s.direction:8} n={s.n_markets} "
            f"bid_sum={s.sum_best_bid:.4f} ask_sum={s.sum_best_ask:.4f}{other}  "
            f"{s.title}"
        )


@app.command()
def stats(db_path: Path = DEFAULT_DB) -> None:
    """Print row counts and the most recent flagged signals."""
    conn = db.connect(db_path)
    db.init_schema(conn)
    for table in ("events", "markets", "market_snapshots", "event_arb_signals"):
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        typer.echo(f"{table:25} {n}")

    typer.echo("\nlatest 10 flagged signals (|gap| >= 0.02):")
    rows = conn.execute(
        """
        SELECT s.detected_at, s.direction, s.n_markets, s.bid_gap, s.ask_gap,
               e.title
        FROM event_arb_signals s
        JOIN events e ON e.id = s.event_id
        WHERE s.bid_gap > 0.02 OR s.ask_gap > 0.02
        ORDER BY s.detected_at DESC, MAX(s.bid_gap, s.ask_gap) DESC
        LIMIT 10
        """
    ).fetchall()
    for r in rows:
        gap = max(r["bid_gap"], r["ask_gap"])
        typer.echo(
            f"  {r['detected_at'][:19]}  {r['direction']:8}  "
            f"gap={gap:+.4f} n={r['n_markets']}  {r['title']}"
        )


if __name__ == "__main__":
    app()
