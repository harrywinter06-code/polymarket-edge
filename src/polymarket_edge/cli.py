"""CLI for polymarket-edge."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from polymarket_edge import (
    analysis,
    db,
    detector,
    fetch,
    hl_backtest,
    hyperliquid,
    monitor,
    paper,
    report,
)

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


@app.command("monitor")
def monitor_cmd(
    db_path: Path = DEFAULT_DB,
    duration_minutes: float = typer.Option(30.0, help="How long to poll"),
    poll_interval: float = typer.Option(60.0, help="Seconds between polls"),
    max_events_per_poll: int = typer.Option(
        300, help="Cap per poll (0 = no cap; full unbounded fetch can OOM)"
    ),
) -> None:
    """Poll active events at a fixed cadence, recording signal trajectories.

    Each invocation gets a unique poll_run_id; use `persistence` to analyze.
    """
    cap = max_events_per_poll or None
    poll_run_id, n_polls, n_written = asyncio.run(
        monitor.run_monitor(
            str(db_path),
            duration_minutes=duration_minutes,
            poll_interval_seconds=poll_interval,
            max_events_per_poll=cap,
        )
    )
    typer.echo(
        f"poll_run_id={poll_run_id}  polls={n_polls}  trajectories_written={n_written}"
    )


@app.command()
def persistence(
    db_path: Path = DEFAULT_DB,
    poll_run_id: str = typer.Option("", help="Specific run; blank = most recent"),
    threshold: float = typer.Option(0.005, help="Entry threshold for forward-test"),
    hold_seconds: float = typer.Option(300.0, help="Forward-test hold duration"),
) -> None:
    """Print persistence + forward-test stats for an observation window."""
    conn = db.connect(db_path)
    db.init_schema(conn)
    runs = monitor.list_poll_runs(conn)
    if not runs:
        typer.echo("no poll runs found")
        raise typer.Exit(1)
    if not poll_run_id:
        poll_run_id = runs[0][0]
    rows = monitor.fetch_trajectories(conn, poll_run_id=poll_run_id)
    traj = analysis.to_trajectories(rows)
    ps = analysis.persistence_stats(traj)
    typer.echo(f"poll_run_id={poll_run_id}")
    typer.echo(
        f"  snapshots={ps.n_snapshots}  distinct_events={ps.n_distinct_events}"
    )
    typer.echo(
        f"  |gap|  mean={ps.gap_mean:.4f}  p50={ps.gap_p50:.4f}  "
        f"p90={ps.gap_p90:.4f}  p99={ps.gap_p99:.4f}  max={ps.gap_max:.4f}"
    )
    typer.echo("threshold-crossings (distinct events that ever crossed):")
    for tc in analysis.threshold_counts(traj):
        typer.echo(f"  >= {tc.threshold:.4f}: {tc.n_events_ever_crossed}")
    ft = analysis.forward_test(traj, threshold=threshold, hold_seconds=hold_seconds)
    typer.echo(
        f"forward-test (entry |gap|>={ft.threshold:.4f}, hold>={ft.hold_seconds:.0f}s):"
    )
    typer.echo(
        f"  n_entries={ft.n_entries}  "
        f"mean_realized_gap_at_close={ft.mean_realized_gap_at_close:+.4f}  "
        f"mean_decay_toward_zero={ft.mean_gap_decay:+.4f}"
    )


@app.command()
def runs(db_path: Path = DEFAULT_DB) -> None:
    """List all observation runs."""
    conn = db.connect(db_path)
    db.init_schema(conn)
    for run_id, n, first_at, last_at in monitor.list_poll_runs(conn):
        typer.echo(f"  {run_id}  rows={n:5}  {first_at[:19]} -> {last_at[:19]}")


# ---------- Hyperliquid (days 3-4) ----------


@app.command("hl-ingest")
def hl_ingest_cmd(db_path: Path = DEFAULT_DB) -> None:
    """Snapshot the current Hyperliquid universe + funding for every perp."""
    universe, ctxs = asyncio.run(hyperliquid.fetch_meta_and_ctxs())
    conn = db.connect(db_path)
    db.init_schema(conn)
    fetched_at = hyperliquid.now_iso()
    hyperliquid.upsert_universe(conn, universe, fetched_at)
    n = 0
    for u, ctx in zip(universe, ctxs, strict=True):
        coin = u.get("name")
        if not coin or "funding" not in ctx:
            continue
        hyperliquid.insert_funding_snapshot(conn, coin=coin, ctx=ctx, snapshot_at=fetched_at)
        n += 1
    conn.commit()
    typer.echo(f"snapshot: {len(universe)} coins, {n} funding rows -> {db_path}")
    sortable = [
        (u.get("name"), float(ctx["funding"]))
        for u, ctx in zip(universe, ctxs, strict=True)
        if "funding" in ctx
    ]
    sortable.sort(key=lambda kv: kv[1], reverse=True)
    typer.echo("top 10 by current hourly funding (annualized):")
    for coin, f in sortable[:10]:
        typer.echo(f"  {coin:10}  {f:+.6f}/hr  ({hyperliquid.annualize(f) * 100:+.1f}% APR)")


@app.command("hl-history")
def hl_history_cmd(
    db_path: Path = DEFAULT_DB,
    coins: str = typer.Option(
        "",
        help="Comma-separated coin list. Blank = top 30 by open interest (current snapshot).",
    ),
    days: int = typer.Option(30, help="Historical window in days"),
    top_n_by_oi: int = typer.Option(30, help="If --coins blank: top N coins by open interest"),
) -> None:
    """Pull `days` of hourly funding history for a coin set and persist."""
    conn = db.connect(db_path)
    db.init_schema(conn)

    if coins.strip():
        coin_list = [c.strip().upper() for c in coins.split(",") if c.strip()]
    else:
        rows = conn.execute(
            """
            SELECT coin, MAX(open_interest) AS oi
            FROM hl_funding_snapshots
            GROUP BY coin
            ORDER BY oi DESC NULLS LAST
            LIMIT ?
            """,
            (top_n_by_oi,),
        ).fetchall()
        coin_list = [r[0] for r in rows]
        if not coin_list:
            typer.echo("no snapshot data; run `hl-ingest` first")
            raise typer.Exit(1)

    typer.echo(f"pulling {days}d of funding for {len(coin_list)} coins...")
    series_map = asyncio.run(
        hyperliquid.fetch_funding_history_many(coin_list, days=days)
    )
    fetched_at = hyperliquid.now_iso()
    total = 0
    for coin, rows in series_map.items():
        total += hyperliquid.insert_funding_history(conn, coin, rows, fetched_at)
    conn.commit()
    typer.echo(f"persisted {total} funding-history rows for {len(coin_list)} coins")


@app.command("hl-backtest")
def hl_backtest_cmd(
    db_path: Path = DEFAULT_DB,
    top_k: int = typer.Option(5, help="Number of coins to short each rebalance"),
    trailing_hours: int = typer.Option(24, help="Trailing window for predictor"),
    rebalance_hours: int = typer.Option(8, help="Hold period per rebalance"),
    benchmark_coin: str = typer.Option("BTC", help="Coin for passive-short baseline"),
) -> None:
    """Run the funding-capture backtest over all stored historical funding."""
    conn = db.connect(db_path)
    db.init_schema(conn)
    ticks = hl_backtest.load_funding(conn)
    if not ticks:
        typer.echo("no funding history; run `hl-history` first")
        raise typer.Exit(1)

    strat = hl_backtest.backtest_top_k_trailing(
        ticks,
        top_k=top_k,
        trailing_hours=trailing_hours,
        rebalance_hours=rebalance_hours,
    )
    perfect = hl_backtest.backtest_perfect_hindsight(
        ticks, top_k=top_k, rebalance_hours=rebalance_hours
    )
    passive = hl_backtest.backtest_passive(
        ticks, coin=benchmark_coin, rebalance_hours=rebalance_hours
    )

    typer.echo(f"funding ticks loaded: {len(ticks):,}")
    typer.echo(
        f"{'strategy':45} {'n_reb':>6} {'tot_ret':>9} {'ann_ret':>9} "
        f"{'ann_vol':>9} {'sharpe':>7} {'mdd':>8} {'hit%':>6} {'coins':>6}"
    )
    for r in (strat, perfect, passive):
        typer.echo(
            f"{r.strategy:45} {r.n_rebalances:>6} "
            f"{r.total_return:>+9.4f} {r.annualized_return:>+9.4f} "
            f"{r.annualized_vol:>9.4f} {r.sharpe:>+7.2f} "
            f"{r.max_drawdown:>8.4f} {r.hit_rate * 100:>5.1f}% "
            f"{r.n_distinct_coins_held:>6}"
        )
    typer.echo(
        "\nNote: annualized return assumes funding is the only P&L source. "
        "Real net P&L is lower (basis risk, spot funding, slippage, liquidation buffer)."
    )


# ---------- Day 5: paper-trading + research note ----------


@app.command("paper-auto")
def paper_auto_cmd(
    db_path: Path = DEFAULT_DB,
    fee_buffer: float = typer.Option(0.005, help="Min |gap| to open a position"),
    notional_usd: float = typer.Option(100.0, help="USD notional per position"),
    close_decay: float = typer.Option(0.5, help="Close when |gap| <= decay * |entry_gap|"),
    max_events: int = typer.Option(300, help="Cap per round (0 = no cap; can OOM)"),
) -> None:
    """One paper-trading round: open new flagged events, mark + close decayed positions."""
    cap = max_events or None
    n_open, n_close, n_marked = asyncio.run(
        paper.paper_auto_round(
            str(db_path),
            fee_buffer=fee_buffer,
            notional_usd=notional_usd,
            close_decay=close_decay,
            max_events=cap,
        )
    )
    typer.echo(f"opened={n_open}  closed={n_close}  marked_open={n_marked}")


@app.command("paper-pnl")
def paper_pnl_cmd(db_path: Path = DEFAULT_DB) -> None:
    """Print paper-trading P&L summary."""
    conn = db.connect(db_path)
    db.init_schema(conn)
    s = paper.paper_pnl_summary(conn)
    for k, v in s.items():
        typer.echo(f"  {k:30}  {v}")


@app.command("report")
def report_cmd(
    db_path: Path = DEFAULT_DB,
    out: Path = typer.Option(Path("REPORT.md"), help="Output markdown path"),  # noqa: B008
) -> None:
    """Generate a markdown research note from the SQLite store."""
    conn = db.connect(db_path)
    db.init_schema(conn)
    p = report.write_report(conn, out)
    typer.echo(f"wrote {p}")


if __name__ == "__main__":
    app()
