"""CLI for polymarket-edge."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from polymarket_edge import (
    analysis,
    book_depth,
    dashboard,
    db,
    detector,
    fetch,
    hl_backtest,
    hl_stats,
    hl_stats_block,
    hyperliquid,
    microstructure,
    monitor,
    paper,
    report,
    walkforward,
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
        100,
        help="Cap per poll. Larger caps require a host with enough virtual "
        "memory; the gamma /events embedded-markets payload is heavy.",
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
    total_ok, total_bad = 0, 0
    for coin, rows in series_map.items():
        ok, bad = hyperliquid.insert_funding_history(conn, coin, rows, fetched_at)
        total_ok += ok
        total_bad += bad
    conn.commit()
    typer.echo(
        f"persisted {total_ok} funding-history rows for {len(coin_list)} coins"
        f" (dropped {total_bad} malformed)"
    )


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
    max_age_hours: float = typer.Option(168.0, help="Hard close at this age regardless of decay"),
    max_events: int = typer.Option(100, help="Cap per round (larger caps may OOM)"),
) -> None:
    """One paper-trading round: open new flagged events, mark + close decayed positions."""
    cap = max_events or None
    n_open, n_close, n_marked = asyncio.run(
        paper.paper_auto_round(
            str(db_path),
            fee_buffer=fee_buffer,
            notional_usd=notional_usd,
            close_decay=close_decay,
            max_age_hours=max_age_hours,
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


@app.command()
def depth(
    slug: str = typer.Argument(..., help="Event slug to inspect"),
    notionals: str = typer.Option("10,50,100,500,1000", help="Comma-sep USD per market"),
) -> None:
    """Walk the order book on every market in a negRisk event and report the
    depth-aware basket gap at multiple notionals."""
    events = asyncio.run(fetch.fetch_all_active_events(max_events=500))
    ev = next((e for e in events if e.get("slug") == slug), None)
    if ev is None:
        typer.echo(f"event slug not found: {slug}")
        raise typer.Exit(1)
    if not ev.get("negRisk"):
        typer.echo(f"warning: event {slug!r} is not negRisk; basket math may not apply")
    active = [
        m for m in ev.get("markets", [])
        if m.get("active") and not m.get("closed") and m.get("acceptingOrders")
    ]
    typer.echo(f"event: {ev.get('title')}")
    typer.echo(f"  negRisk={ev.get('negRisk')} negRiskAugmented={ev.get('negRiskAugmented')}")
    typer.echo(f"  n_active_markets={len(active)}")
    sum_bid = sum(float(m['bestBid']) for m in active if m.get('bestBid') is not None)
    sum_ask = sum(float(m['bestAsk']) for m in active if m.get('bestAsk') is not None)
    typer.echo(
        f"  top-of-book: sum_bid={sum_bid:.4f}  bid_gap={sum_bid - 1:+.4f}  "
        f"sum_ask={sum_ask:.4f}  ask_gap={1 - sum_ask:+.4f}"
    )

    typer.echo("\nfetching order books for every active market...")
    books = asyncio.run(book_depth.fetch_books_for_event(active))
    typer.echo(f"  fetched {len(books)} books")

    sides = []
    if sum_bid > 1.0:
        sides.append(("sell_yes", book_depth.basket_sell_yes_depth))
    if sum_ask < 1.0:
        sides.append(("buy_yes", book_depth.basket_buy_yes_depth))
    if not sides:
        typer.echo("\nno flagged direction at top of book; nothing to walk.")
        return

    for side_name, fn in sides:
        typer.echo(f"\n{side_name} basket sweep:")
        typer.echo(
            f"  {'notional/mkt':>14} {'sum_top':>9} {'sum_depth':>10} "
            f"{'gap_top':>9} {'gap_depth':>10} {'throttle_usd':>13}  throttle_market"
        )
        for n_str in notionals.split(","):
            n = float(n_str.strip())
            r = fn(active, books, notional_per_market_usd=n)
            typer.echo(
                f"  {r.notional_per_market_usd:>14.2f} "
                f"{r.sum_top_of_book:>9.4f} {r.sum_avg_fill:>10.4f} "
                f"{r.gap_top_of_book:>+9.4f} {r.gap_depth_aware:>+10.4f} "
                f"{r.basket_throttle_notional:>13.2f}  "
                f"{r.basket_throttle_market}"
            )


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


@app.command("walk-forward")
def walk_forward_cmd(
    db_path: Path = DEFAULT_DB,
    train_days: int = typer.Option(10),
    test_days: int = typer.Option(5),
    step_days: int = typer.Option(3),
    top_k: int = typer.Option(5),
    trailing_hours: int = typer.Option(24),
    rebalance_hours: int = typer.Option(8),
) -> None:
    """Walk-forward (out-of-sample) backtest. Default config fits the ~20-day
    common grid on the live DB; the README's nominal 15/7 needs ~22 days."""
    conn = db.connect(db_path)
    db.init_schema(conn)
    ticks = hl_backtest.load_funding(conn)
    if not ticks:
        typer.echo("no funding history; run `hl-history` first")
        raise typer.Exit(1)
    r = walkforward.walk_forward_top_k_trailing(
        ticks,
        train_days=train_days,
        test_days=test_days,
        step_days=step_days,
        top_k=top_k,
        trailing_hours=trailing_hours,
        rebalance_hours=rebalance_hours,
    )
    typer.echo(
        f"strategy: {r.strategy}  windows={r.n_windows}  "
        f"IS mean ann={r.in_sample_ann_ret_mean:+.4f}  "
        f"OOS mean ann={r.out_of_sample_ann_ret_mean:+.4f}  "
        f"decay (IS-OOS, pp)={r.is_oos_decay_pp:+.4f}"
    )
    for w in r.windows:
        typer.echo(
            f"  train={w.n_train_periods}p test={w.n_test_periods}p  "
            f"IS={w.in_sample_annualized:+.4f}  OOS={w.out_of_sample_annualized:+.4f}  "
            f"IS_Sharpe={w.in_sample_sharpe:+.2f}  OOS_Sharpe={w.out_of_sample_sharpe:+.2f}  "
            f"carried={w.coins_carried_to_test}/{w.coins_held_in_train}"
        )


@app.command("hl-ci-block")
def hl_ci_block_cmd(
    db_path: Path = DEFAULT_DB,
    top_k: int = typer.Option(5),
    trailing_hours: int = typer.Option(24),
    rebalance_hours: int = typer.Option(8),
    n_resamples: int = typer.Option(5000),
) -> None:
    """Block-bootstrap 95% CI (preserves funding autocorrelation)."""
    conn = db.connect(db_path)
    db.init_schema(conn)
    ticks = hl_backtest.load_funding(conn)
    if not ticks:
        typer.echo("no funding history; run `hl-history` first")
        raise typer.Exit(1)
    returns = hl_stats.compute_per_period_returns_trailing(
        ticks, top_k=top_k, trailing_hours=trailing_hours, rebalance_hours=rebalance_hours,
    )
    block_len = hl_stats_block.estimate_optimal_block_length(returns)
    iid = hl_stats.bootstrap_backtest_stats(
        returns, hours_per_period=rebalance_hours, n_resamples=n_resamples
    )
    mb = hl_stats_block.moving_block_bootstrap(
        returns, hours_per_period=rebalance_hours,
        block_length=block_len, n_resamples=n_resamples,
    )
    sb = hl_stats_block.stationary_bootstrap(
        returns, hours_per_period=rebalance_hours,
        block_length=float(block_len), n_resamples=n_resamples,
    )
    typer.echo(
        f"n_periods={len(returns)}  optimal_block_length={block_len}"
    )
    for label, s in (("IID", iid), ("moving-block", mb), ("stationary", sb)):
        ar = s.annualized_return
        sh = s.sharpe
        typer.echo(
            f"  {label:13}  ann={ar.point:+.4f} CI[{ar.ci_low:+.4f},{ar.ci_high:+.4f}]  "
            f"sharpe={sh.point:+.2f} CI[{sh.ci_low:+.2f},{sh.ci_high:+.2f}]"
        )


@app.command("microstructure-scan")
def microstructure_scan_cmd(
    db_path: Path = DEFAULT_DB,
    max_events: int = typer.Option(500),
    small_size_usd: float = typer.Option(50.0),
    med_size_usd: float = typer.Option(500.0),
    fee_buffer: float = typer.Option(0.005),
) -> None:
    """Scan all currently-active negRisk events and classify each as
    real / marginal / trap by depth-aware basket P&L."""
    classifications = asyncio.run(
        microstructure.scan_and_classify(
            max_events=max_events,
            small_size_usd=small_size_usd,
            med_size_usd=med_size_usd,
            fee_buffer=fee_buffer,
        )
    )
    by_cat = microstructure.aggregate_by_category(classifications)
    n = len(classifications)
    if n == 0:
        typer.echo("no flagged events")
        return
    traps = sum(1 for c in classifications if c.verdict == "trap")
    reals = sum(1 for c in classifications if c.verdict == "real")
    marginals = sum(1 for c in classifications if c.verdict == "marginal")
    typer.echo(f"flagged={n}  real={reals}  marginal={marginals}  trap={traps}")
    typer.echo(f"trap_rate={traps / n:.1%}")
    typer.echo("by category:")
    for cat, counts in sorted(by_cat.items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(counts.values())
        trap_rate = counts.get("trap", 0) / total if total else 0.0
        typer.echo(
            f"  {cat:20}  total={total}  trap={counts.get('trap', 0)}  "
            f"trap_rate={trap_rate:.1%}"
        )


@app.command("dashboard")
def dashboard_cmd(
    db_path: Path = DEFAULT_DB,
    out: Path = typer.Option(Path("dashboard.html"), help="Output HTML path"),  # noqa: B008
) -> None:
    """Generate the single-file HTML dashboard (embeds charts as base64)."""
    conn = db.connect(db_path)
    db.init_schema(conn)
    p = dashboard.write_dashboard(conn, out)
    typer.echo(f"wrote {p}")


@app.command("hl-ci")
def hl_ci_cmd(
    db_path: Path = DEFAULT_DB,
    top_k: int = typer.Option(5),
    trailing_hours: int = typer.Option(24),
    rebalance_hours: int = typer.Option(8),
    n_resamples: int = typer.Option(5000),
    spread_bps_per_leg: float = typer.Option(0.0, help="Net P&L when >0"),
) -> None:
    """Bootstrap 95% CI on annualized return and Sharpe."""
    conn = db.connect(db_path)
    db.init_schema(conn)
    ticks = hl_backtest.load_funding(conn)
    if not ticks:
        typer.echo("no funding history; run `hl-history` first")
        raise typer.Exit(1)
    returns = hl_stats.compute_per_period_returns_trailing(
        ticks,
        top_k=top_k,
        trailing_hours=trailing_hours,
        rebalance_hours=rebalance_hours,
    )
    if spread_bps_per_leg > 0:
        cost_per_rebalance = 4 * spread_bps_per_leg / 10_000
        returns = [r - cost_per_rebalance for r in returns]
    stats = hl_stats.bootstrap_backtest_stats(
        returns,
        hours_per_period=rebalance_hours,
        n_resamples=n_resamples,
    )
    label = f"top-{top_k} trail-{trailing_hours}h rebal-{rebalance_hours}h"
    if spread_bps_per_leg > 0:
        label += f" net of {spread_bps_per_leg}bp/leg"
    typer.echo(f"strategy: {label}")
    typer.echo(f"  n_periods={len(returns)}  n_resamples={stats.n_resamples}")
    typer.echo(
        f"  annualized_return: point={stats.annualized_return.point:+.4f}  "
        f"95% CI [{stats.annualized_return.ci_low:+.4f}, "
        f"{stats.annualized_return.ci_high:+.4f}]"
    )
    typer.echo(
        f"  sharpe:            point={stats.sharpe.point:+.2f}  "
        f"95% CI [{stats.sharpe.ci_low:+.2f}, {stats.sharpe.ci_high:+.2f}]"
    )


if __name__ == "__main__":
    app()
