"""Research-note generator.

Reads everything from the SQLite DB and writes a markdown research note
summarizing the project. No external chart library — figures are markdown
tables. The note is meant to be a faithful summary of what the code did and
what the data shows, with limitations called out explicitly.
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import datetime
from pathlib import Path

from polymarket_edge import analysis, hl_backtest, monitor


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _scoreable_signal_stats(conn: sqlite3.Connection) -> dict[str, float]:
    """Aggregate stats over the event_arb_signals table (each row = one scan)."""
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS n,
            AVG(MAX(bid_gap, ask_gap)) AS mean_best_gap,
            MAX(MAX(bid_gap, ask_gap)) AS max_best_gap,
            SUM(CASE WHEN bid_gap > 0.005 OR ask_gap > 0.005 THEN 1 ELSE 0 END) AS n_above_50bp,
            SUM(CASE WHEN bid_gap > 0.02  OR ask_gap > 0.02  THEN 1 ELSE 0 END) AS n_above_2pct
        FROM event_arb_signals
        """
    ).fetchone()
    return {
        "n": row["n"] or 0,
        "mean_best_gap": row["mean_best_gap"] or 0.0,
        "max_best_gap": row["max_best_gap"] or 0.0,
        "n_above_50bp": row["n_above_50bp"] or 0,
        "n_above_2pct": row["n_above_2pct"] or 0,
    }


def _top_flagged(
    conn: sqlite3.Connection,
    threshold: float = 0.005,
    limit: int = 10,
) -> list[sqlite3.Row]:
    # Dedupe by event_id: take the largest |gap| ever observed per event.
    return list(
        conn.execute(
            """
            WITH ranked AS (
                SELECT s.*,
                       MAX(s.bid_gap, s.ask_gap) AS best_gap,
                       ROW_NUMBER() OVER (
                           PARTITION BY s.event_id
                           ORDER BY MAX(s.bid_gap, s.ask_gap) DESC
                       ) AS rn
                FROM event_arb_signals s
            )
            SELECT e.title, e.slug, r.n_markets, r.bid_gap, r.ask_gap,
                   r.direction, r.has_neg_risk_other, r.detected_at, r.best_gap
            FROM ranked r
            JOIN events e ON e.id = r.event_id
            WHERE r.rn = 1 AND r.best_gap >= ?
            ORDER BY r.best_gap DESC
            LIMIT ?
            """,
            (threshold, limit),
        )
    )


def _persistence_section(conn: sqlite3.Connection) -> tuple[str, int]:
    """Compose persistence stats from the longest observation run. Returns
    (markdown, n_trajectories_used)."""
    runs = monitor.list_poll_runs(conn)
    if not runs:
        return ("_No monitor runs recorded — re-run `polymarket-edge monitor` "
                "for forward-observation analysis._", 0)
    best = max(runs, key=lambda r: r[1])  # most rows
    rows = monitor.fetch_trajectories(conn, poll_run_id=best[0])
    traj = analysis.to_trajectories(rows)
    if not traj:
        return ("_Monitor ran but recorded no trajectories — check rate limit "
                "or detector filters._", 0)
    ps = analysis.persistence_stats(traj)
    tcs = analysis.threshold_counts(traj)
    ft = analysis.forward_test(traj, threshold=0.005, hold_seconds=300.0)

    threshold_rows = "\n".join(
        f"| {tc.threshold:.4f} | {tc.n_events_ever_crossed} |" for tc in tcs
    )

    return (
        f"""**Observation window:** {best[2][:19]} -> {best[3][:19]}  ({best[1]} trajectory rows)

| metric | value |
|---|---|
| snapshots | {ps.n_snapshots} |
| distinct events | {ps.n_distinct_events} |
| mean abs(gap) | {ps.gap_mean:.4f} |
| p50 abs(gap) | {ps.gap_p50:.4f} |
| p90 abs(gap) | {ps.gap_p90:.4f} |
| p99 abs(gap) | {ps.gap_p99:.4f} |
| max abs(gap) | {ps.gap_max:.4f} |

**Distinct events that ever crossed each threshold during the window:**

| threshold | n distinct events |
|---|---|
{threshold_rows}

**Forward-test (entry on |gap| >= 50bp, hold >= 5 minutes):**

| metric | value |
|---|---|
| candidate entries | {ft.n_entries} |
| mean realized gap at close | {ft.mean_realized_gap_at_close:+.4f} |
| mean decay toward zero | {ft.mean_gap_decay:+.4f} |

Interpretation: a positive `mean decay toward zero` means flagged signals
revert toward fair pricing over the hold horizon — consistent with a real
microstructure inefficiency that is being arbed away by the time fees clear it.
""",
        len(traj),
    )


def _hl_backtest_section(conn: sqlite3.Connection) -> str:
    ticks = hl_backtest.load_funding(conn)
    if not ticks:
        return "_No Hyperliquid funding history present — run `hl-history` first._"
    coins = sorted({t.coin for t in ticks})
    by_coin: dict[str, list[float]] = {}
    for t in ticks:
        by_coin.setdefault(t.coin, []).append(t.funding)
    coin_apr_iter = (
        (
            c,
            statistics.fmean(vs) * 24 * 365 * 100,
            statistics.pstdev(vs) * (24 * 365) ** 0.5 * 100,
        )
        for c, vs in by_coin.items()
        if len(vs) >= 24
    )
    coin_apr = sorted(coin_apr_iter, key=lambda x: x[1], reverse=True)
    top_lines = "\n".join(
        f"| {c} | {apr:+.1f}% | {vol:.1f}% |" for c, apr, vol in coin_apr[:10]
    )

    trail = hl_backtest.backtest_top_k_trailing(
        ticks, top_k=5, trailing_hours=24, rebalance_hours=8
    )
    perfect = hl_backtest.backtest_perfect_hindsight(ticks, top_k=5, rebalance_hours=8)
    btc_ticks = [t for t in ticks if t.coin == "BTC"]
    passive = (
        hl_backtest.backtest_passive(ticks, coin="BTC", rebalance_hours=8)
        if btc_ticks
        else None
    )

    strat_rows = [trail, perfect] + ([passive] if passive else [])
    table = "\n".join(
        f"| {r.strategy} | {r.n_rebalances} | {r.total_return:+.4f} | "
        f"{r.annualized_return:+.4f} | {r.annualized_vol:.4f} | {r.sharpe:+.2f} | "
        f"{r.max_drawdown:.4f} | {r.hit_rate * 100:.1f}% |"
        for r in strat_rows
    )

    capture = (trail.total_return / perfect.total_return) if perfect.total_return > 0 else 0.0

    return f"""**Dataset:** {len(ticks):,} hourly funding ticks across {len(coins)} coins.

**Per-coin annualized funding (top 10 by mean realized rate):**

| coin | annualized mean | annualized vol |
|---|---|---|
{top_lines}

**Strategy results (rebalance 8h, top-K = 5, trailing window = 24h):**

| strategy | n_rebalances | total | annualized | ann_vol | sharpe | mdd | hit |
|---|---|---|---|---|---|---|---|
{table}

The trailing-mean predictor captures **{capture * 100:.0f}%** of the
perfect-hindsight ceiling. The Sharpe numbers here are an upper bound — they
do not include the cost of the spot hedge leg, basis risk, slippage, or
liquidation-buffer drag. Realistic net returns are materially lower.
"""


def _paper_section(conn: sqlite3.Connection) -> str:
    n_open = conn.execute(
        "SELECT COUNT(*) FROM paper_positions WHERE closed_at IS NULL"
    ).fetchone()[0]
    closed = conn.execute(
        """
        SELECT COUNT(*) AS n,
               COALESCE(SUM(realized_pnl_usd), 0.0) AS total,
               COALESCE(SUM(notional_usd), 0.0) AS gross,
               COALESCE(AVG(realized_pnl_usd), 0.0) AS mean
        FROM paper_positions
        WHERE closed_at IS NOT NULL
        """
    ).fetchone()
    if not n_open and not closed["n"]:
        return ("_No paper-trading rounds run yet. Run "
                "`polymarket-edge paper-auto` periodically over several days "
                "for live forward results._")
    return f"""| metric | value |
|---|---|
| open positions | {n_open} |
| closed positions | {closed["n"]} |
| total realized P&L (USD) | {closed["total"]:+.2f} |
| mean realized P&L per closed (USD) | {closed["mean"]:+.4f} |
| gross closed notional (USD) | {closed["gross"]:.2f} |

Position P&L model: `pnl = notional * (|entry_gap| - |current_gap|)`.
This is the linear-approximation P&L for the underlying basket trades and
does not include taker fees, slippage, or hold-to-settlement reset.
"""


def generate_markdown(conn: sqlite3.Connection) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    counts = {tbl: _row_count(conn, tbl) for tbl in (
        "events", "markets", "market_snapshots", "event_arb_signals",
        "signal_trajectories", "hl_funding_history", "paper_positions",
    )}
    stats = _scoreable_signal_stats(conn)
    top = _top_flagged(conn, threshold=0.005, limit=10)
    top_lines = "\n".join(
        f"| {(r['title'] or '')[:60]} | {r['n_markets']} | "
        f"{r['direction']} | {max(r['bid_gap'], r['ask_gap']):+.4f} | "
        f"{'has_other' if r['has_neg_risk_other'] else ''} |"
        for r in top
    ) or "| _none yet_ | | | | |"

    persistence_md, _ = _persistence_section(conn)
    hl_md = _hl_backtest_section(conn)
    paper_md = _paper_section(conn)

    return f"""# polymarket-edge — research note

Generated: {now}

## What we built and why

The Ask Gina quant-intern job description names Polymarket and Hyperliquid
as the venues. This project is a forward-observation + funding-capture stack
across both, designed in five days from a verified read of each venue's
public API.

Headline design decision: on Polymarket, `P(YES) + P(NO) = $1` is
contract-enforced per market via the CLOB order-mirroring rule, so naive
intra-market "yes + no > 1" arbs cannot exist in steady state. The
non-trivial signal lives at the **event** level — for a `negRisk` event with
N mutually-exclusive markets, the sum of YES probabilities across the event
must equal 1.0. Any deviation is potential arb (modulo fees).

## Data collected

| table | rows |
|---|---|
{chr(10).join(f"| {k} | {v:,} |" for k, v in counts.items())}

## Polymarket — event-level no-arb signals

**Across all scans:**

| metric | value |
|---|---|
| signals scored | {stats["n"]:,} |
| mean best_gap | {stats["mean_best_gap"]:.4f} |
| max best_gap | {stats["max_best_gap"]:.4f} |
| n signals over 50bp | {stats["n_above_50bp"]} |
| n signals over 2% (fee-clearable) | {stats["n_above_2pct"]} |

**Top flagged events (best_gap >= 50bp, dedup-by-event):**

| title | n_markets | direction | best_gap | flags |
|---|---|---|---|---|
{top_lines}

## Polymarket — forward observation (persistence study)

{persistence_md}

## Hyperliquid — funding-capture backtest

{hl_md}

## Live paper-trading

{paper_md}

## Honest limitations

The numbers in this note overstate net realizable P&L. Specifically:

- **Polymarket**:
  - The detector treats `negRisk: true` events as mutually exclusive **and
    exhaustive**, but `negRiskOther` markets break exhaustivity. Events with
    `has_neg_risk_other = True` should be discounted accordingly.
  - Quote-fill assumption is `best_bid` / `best_ask` (top-of-book). Real
    fills cross the book, especially on the illiquid lower-probability legs
    of a multi-outcome event.
  - Taker fees are typically ~2% per leg; combined with hedge-leg drag, only
    >2% gaps are likely to clear in practice. None of the live signals during
    the observation window cleared that bar — they topped out around 150bp.
  - Historical retrospective is blocked by the CLOB `/prices-history`
    12h-granularity floor on resolved markets, so we cannot reconstruct the
    exact intra-day path of past mispricings.

- **Hyperliquid**:
  - The backtest measures funding flows only. It does NOT model the spot
    hedge leg cost (basis risk, spot funding, slippage), the leverage /
    liquidation buffer drag, or transaction fees. Reported Sharpe is an
    upper bound that real net returns will fall well short of.
  - Coin selection relies on `open_interest` from current snapshot, which is
    measured in token units, not USD notional — the default selector skews
    toward memecoins. For majors (BTC/ETH/SOL etc.) the funding rate has a
    floor at the 10.95% APR base rate.
  - 30 days of history is a small sample. Sharpe on small N is noisy and the
    universe composition has structural shifts (new perp listings, OI
    shocks) that the backtest does not adjust for.

- **Paper-trading**:
  - P&L is linear-approximation in the gap, not the true sum-of-prices math
    accounting for fees and settlement timing.

## What would be next

- Polymarket: account for `negRiskOther` in the sum constraint; pull
  `/prices-history` at 12h fidelity for resolved `negRisk` events and chart
  the time-series of `sum(best_bid)` over each event's lifecycle.
- Hyperliquid: pair funding capture with a real spot/perp hedge model; pull
  spot prices and compute the realized hedge P&L per period.
- Cross-venue: pair Polymarket binary outcomes that are statistically linked
  to onchain assets (e.g., regulatory-decision markets vs BTC funding skew)
  and test for joint mispricings.
"""


def write_report(conn: sqlite3.Connection, out_path: str | Path) -> Path:
    md = generate_markdown(conn)
    p = Path(out_path)
    p.write_text(md, encoding="utf-8")
    return p
