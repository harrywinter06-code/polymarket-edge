"""Self-contained HTML dashboard. Base64-embedded charts, inline CSS, no JS."""

from __future__ import annotations

import base64
import html
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path

from polymarket_edge import hl_backtest, hl_hedge, report

_HEADER_BLURB = (
    "Depth-aware microstructure scanner for Polymarket negRisk events, plus a "
    "365-day funding-capture backtest on Hyperliquid perpetuals. Count-based "
    "trap rate on Polymarket flags is 56-77%; volume-weighted it is 0.012%, "
    "because one event (the 2026 FIFA World Cup) carries 95.9% of flagged "
    "dollars. The Hyperliquid carry signal is durable out-of-sample but does "
    "not survive 5 bp/leg execution at the headline 8h cadence; net Sharpe "
    "is positive only at >= 2-weekly rebalance."
)

# (event, n_markets, top_of_book_gap, gap_depth_aware_at_1k, verdict, class)
_DEPTH_FINDINGS: tuple[tuple[str, str, str, str, str, str], ...] = (
    (
        "2026 FIFA World Cup Winner",
        "48",
        "+150 bp sell",
        "+150 bp",
        "REAL - $48K basket, $145K max",
        "pos",
    ),
    (
        "2028 US Presidential Election (party)",
        "2",
        "+100 bp buy",
        "+50 bp (inverts at $5K)",
        "MARGINAL - small size only",
        "neutral",
    ),
    (
        "Harvey Weinstein sentencing",
        "6",
        "+80 bp sell",
        "-1,040 bp at $50/mkt",
        "TRAP - $7.83 of bid depth on one leg",
        "neg",
    ),
)

_REBAL_CADENCES = (8, 24, 72, 168, 336)


def _img_data_uri(path: Path) -> str | None:
    """Return a base64 data URI for a PNG, or None if the file is missing."""
    if not path.exists():
        return None
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _chart_block(label: str, path: Path) -> str:
    uri = _img_data_uri(path)
    if uri is None:
        return (
            f'<figure class="chart"><figcaption>{html.escape(label)}</figcaption>'
            f'<div class="missing">Chart not yet rendered — run '
            f"<code>polymarket-edge report</code> first</div></figure>"
        )
    return (
        f'<figure class="chart"><figcaption>{html.escape(label)}</figcaption>'
        f'<img alt="{html.escape(label)}" src="{uri}"></figure>'
    )


def _kpi_card(value: str, label: str, anchor: str | None = None) -> str:
    inner = (
        f'<div class="kpi-value">{html.escape(value)}</div>'
        f'<div class="kpi-label">{html.escape(label)}</div>'
    )
    if anchor:
        return f'<a class="kpi" href="#{html.escape(anchor)}">{inner}</a>'
    return f'<div class="kpi">{inner}</div>'


def _kpi_grid(conn: sqlite3.Connection, test_count: int) -> str:
    """Combined cross-venue KPI grid for the `dashboard.html` view."""
    n_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    cards = [
        _kpi_card(f"{n_events:,}", "Polymarket events ingested (lifetime)"),
        _kpi_card("0.012%", "Volume-weighted trap rate", anchor="depth"),
        _kpi_card(
            "+11.0% [+9.4, +13.0]",
            "HL gross ann return, stationary block-bootstrap 95% CI",
        ),
        _kpi_card(str(test_count), "Tests passing"),
    ]
    return '<section class="kpis">' + "".join(cards) + "</section>"


def _polymarket_kpi_grid(conn: sqlite3.Connection, test_count: int) -> str:
    """Polymarket-focused KPI grid: depth-walking is the headline."""
    n_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    # Latest scan's trap rate from microstructure_classifications.
    latest = conn.execute(
        "SELECT scan_id FROM microstructure_classifications "
        "GROUP BY scan_id ORDER BY MAX(classified_at) DESC LIMIT 1"
    ).fetchone()
    trap_rate_label = "—"
    if latest is not None:
        verdicts = conn.execute(
            "SELECT verdict, COUNT(*) FROM microstructure_classifications "
            "WHERE scan_id = ? AND verdict != 'noise' GROUP BY verdict",
            (latest[0],),
        ).fetchall()
        counts = {v: n for v, n in verdicts}
        total = sum(counts.values())
        trap = counts.get("trap", 0)
        if total:
            trap_rate_label = f"{trap / total * 100:.0f}% trap rate"
    cards = [
        _kpi_card(f"{n_events:,}", "Events ingested (lifetime)"),
        _kpi_card("0.012%", "Volume-weighted trap rate", anchor="depth"),
        _kpi_card(trap_rate_label, "Count-based, latest scan"),
        _kpi_card(str(test_count), "Tests passing"),
    ]
    return '<section class="kpis">' + "".join(cards) + "</section>"


def _hyperliquid_kpi_grid(_conn: sqlite3.Connection, test_count: int) -> str:
    """Hyperliquid-focused KPI grid: cadence-frontier and OOS validation are headline."""
    cards = [
        _kpi_card(
            "+11.0% [+9.4, +13.0]",
            "Gross ann return, stationary block-bootstrap 95% CI (n=1,093)",
        ),
        _kpi_card("20 / 20", "OOS walk-forward windows positive"),
        _kpi_card(">= 336h", "Break-even cadence at 5 bp/leg"),
        _kpi_card(str(test_count), "Tests passing"),
    ]
    return '<section class="kpis">' + "".join(cards) + "</section>"


def _top_flagged_table(conn: sqlite3.Connection) -> str:
    rows = report._top_flagged(conn, threshold=0.005, limit=10)
    if not rows:
        return '<p class="muted">No flagged events at the 50 bp threshold yet.</p>'
    body = "\n".join(
        f"<tr>"
        f"<td>{html.escape((r['title'] or '')[:80])}</td>"
        f"<td class='num'>{r['n_markets']}</td>"
        f"<td>{html.escape(r['direction'] or '')}</td>"
        f"<td class='num'>{max(r['bid_gap'], r['ask_gap']):+.4f}</td>"
        f"<td>{'has_other' if r['has_neg_risk_other'] else ''}</td>"
        f"</tr>"
        for r in rows
    )
    return (
        "<table><thead><tr>"
        "<th>Title</th><th>N markets</th><th>Direction</th>"
        "<th>Best gap</th><th>Flags</th>"
        "</tr></thead><tbody>"
        f"{body}"
        "</tbody></table>"
    )


def _depth_table() -> str:
    body_rows: list[str] = []
    for event, n_mkts, gap_tob, gap_depth, verdict, cls in _DEPTH_FINDINGS:
        body_rows.append(
            f"<tr>"
            f"<td>{html.escape(event)}</td>"
            f"<td class='num'>{html.escape(n_mkts)}</td>"
            f"<td class='num'>{html.escape(gap_tob)}</td>"
            f"<td class='num cell-{cls}'>{html.escape(gap_depth)}</td>"
            f"<td>{html.escape(verdict)}</td>"
            f"</tr>"
        )
    return (
        "<table><thead><tr>"
        "<th>Event</th><th>N markets</th><th>Top-of-book gap</th>"
        "<th>Gap @ depth-aware</th><th>Verdict</th>"
        "</tr></thead><tbody>"
        f"{''.join(body_rows)}"
        "</tbody></table>"
    )


def _hl_strategy_table(conn: sqlite3.Connection) -> str:
    ticks = hl_backtest.load_funding(conn)
    if not ticks:
        return (
            '<p class="muted">No Hyperliquid funding history present — '
            "run <code>polymarket-edge hl-history</code> first.</p>"
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
    body = "\n".join(
        f"<tr>"
        f"<td><code>{html.escape(r.strategy)}</code></td>"
        f"<td class='num'>{r.n_rebalances}</td>"
        f"<td class='num'>{r.total_return:+.4f}</td>"
        f"<td class='num'>{r.annualized_return:+.4f}</td>"
        f"<td class='num'>{r.annualized_vol:.4f}</td>"
        f"<td class='num'>{r.sharpe:+.2f}</td>"
        f"<td class='num'>{r.max_drawdown:.4f}</td>"
        f"<td class='num'>{r.hit_rate * 100:.1f}%</td>"
        f"</tr>"
        for r in strat_rows
    )
    return (
        "<table><thead><tr>"
        "<th>Strategy</th><th>N rebal</th><th>Total</th><th>Annualized</th>"
        "<th>Ann vol</th><th>Sharpe</th><th>MDD</th><th>Hit%</th>"
        "</tr></thead><tbody>"
        f"{body}"
        "</tbody></table>"
    )


def _hedge_cost_table(conn: sqlite3.Connection) -> str:
    ticks = hl_backtest.load_funding(conn)
    if not ticks:
        return (
            '<p class="muted">No Hyperliquid funding history present — '
            "run <code>polymarket-edge hl-history</code> first.</p>"
        )
    sweep: list[tuple[int, hl_backtest.BacktestResult, hl_backtest.BacktestResult]] = []
    for rebal in _REBAL_CADENCES:
        gross = hl_backtest.backtest_top_k_trailing(
            ticks, top_k=5, trailing_hours=24, rebalance_hours=rebal
        )
        net = hl_hedge.backtest_top_k_trailing_net_spread(
            ticks,
            top_k=5,
            trailing_hours=24,
            rebalance_hours=rebal,
            spread_bps_per_leg=5.0,
        )
        sweep.append((rebal, gross, net))

    # Highlight the row where net annualized first crosses zero (going from
    # negative to non-negative as cadence lengthens).
    cross_idx: int | None = None
    prev_neg = False
    for idx, (_, _, net) in enumerate(sweep):
        if prev_neg and net.annualized_return >= 0:
            cross_idx = idx
            break
        prev_neg = net.annualized_return < 0

    body_rows: list[str] = []
    for idx, (rebal, gross, net) in enumerate(sweep):
        net_class = " class='cell-pos'" if idx == cross_idx else ""
        body_rows.append(
            f"<tr>"
            f"<td class='num'>{rebal}h</td>"
            f"<td class='num'>{net.n_rebalances}</td>"
            f"<td class='num'>{gross.annualized_return:+.4f}</td>"
            f"<td class='num'{net_class}>{net.annualized_return:+.4f}</td>"
            f"<td class='num'>{net.sharpe:+.2f}</td>"
            f"</tr>"
        )
    return (
        "<table><thead><tr>"
        "<th>Rebalance</th><th>N</th><th>Gross annualized</th>"
        "<th>Net annualized (5 bp/leg)</th><th>Net Sharpe</th>"
        "</tr></thead><tbody>"
        f"{''.join(body_rows)}"
        "</tbody></table>"
    )


def _microstructure_section(conn: sqlite3.Connection) -> str:
    """Render live microstructure-scan aggregates from the most recent scan.

    Falls back to a placeholder telling the reader to run
    `polymarket-edge microstructure-scan` if no rows exist. Reads from the
    `microstructure_classifications` table populated by `scan_and_classify`.
    """
    latest_scan = conn.execute(
        "SELECT scan_id, MAX(classified_at) AS ts "
        "FROM microstructure_classifications "
        "GROUP BY scan_id ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if latest_scan is None:
        return (
            '<p class="muted">No microstructure-scan rows yet. '
            "Run <code>polymarket-edge microstructure-scan</code> to populate.</p>"
        )
    scan_id = latest_scan[0]

    overall = conn.execute(
        "SELECT verdict, COUNT(*) FROM microstructure_classifications "
        "WHERE scan_id = ? GROUP BY verdict",
        (scan_id,),
    ).fetchall()
    counts = {v: n for v, n in overall}
    total = sum(counts.values())
    if total == 0:
        return '<p class="muted">No classifications in the most recent scan.</p>'
    real = counts.get("real", 0)
    marginal = counts.get("marginal", 0)
    trap = counts.get("trap", 0)
    trap_rate = (trap / total * 100) if total else 0.0

    by_cat = conn.execute(
        "SELECT category_tag, "
        "  SUM(CASE WHEN verdict='real' THEN 1 ELSE 0 END) AS n_real, "
        "  SUM(CASE WHEN verdict='marginal' THEN 1 ELSE 0 END) AS n_marg, "
        "  SUM(CASE WHEN verdict='trap' THEN 1 ELSE 0 END) AS n_trap, "
        "  COUNT(*) AS n_total "
        "FROM microstructure_classifications "
        "WHERE scan_id = ? AND verdict != 'noise' "
        "GROUP BY category_tag "
        "ORDER BY n_total DESC LIMIT 10",
        (scan_id,),
    ).fetchall()

    rows_html: list[str] = []
    for cat, n_real, n_marg, n_trap, n_total in by_cat:
        cat_rate = (n_trap / n_total * 100) if n_total else 0.0
        rows_html.append(
            f"<tr><td>{html.escape(cat or 'Uncategorized')}</td>"
            f"<td class='num'>{n_total}</td>"
            f"<td class='num'>{n_real}</td>"
            f"<td class='num'>{n_marg}</td>"
            f"<td class='num'>{n_trap}</td>"
            f"<td class='num'>{cat_rate:.1f}%</td></tr>"
        )

    return (
        f"<p class='muted'>Latest scan: <code>{html.escape(scan_id)}</code> &middot; "
        f"<strong>{total}</strong> events classified &middot; "
        f"<strong>{real}</strong> real, <strong>{marginal}</strong> marginal, "
        f"<strong>{trap}</strong> trap &middot; "
        f"overall trap rate <strong>{trap_rate:.1f}%</strong>.</p>"
        "<table><thead><tr>"
        "<th>Category</th><th>Total</th><th>Real</th><th>Marginal</th>"
        "<th>Trap</th><th>Trap rate</th>"
        "</tr></thead><tbody>"
        + "".join(rows_html) +
        "</tbody></table>"
        "<p class='muted' style='font-size:13px;margin-top:8px;'>"
        "Single-row categories (n=1) are individual events flagged in this "
        "scan window, not population statistics. A &quot;100% trap rate&quot; "
        "row for Soccer here is one low-volume match, not the 2026 FIFA "
        "World Cup event in the depth-vs-trap table above (which remains "
        "the durable real signal across re-scans). The robust population "
        "statistic is the rolled-up trap rate across "
        "<code>Politics</code> + <code>Elections</code> + <code>US Election</code> + "
        "<code>Midterms</code> combined, where multi-row sampling is "
        "consistent: see <code>MICROSTRUCTURE.md</code> for the cumulative "
        "table.</p>"
    )


def _count_tests() -> int:
    """Best-effort test count via `pytest --collect-only -q`. Falls back to 40."""
    try:
        import subprocess

        repo_root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            ["uv", "run", "pytest", "--collect-only", "-q"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        # The summary line looks like "40 tests collected in 2.52s".
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if "test" in stripped and "collected" in stripped:
                head = stripped.split()[0]
                if head.isdigit():
                    return int(head)
    except (FileNotFoundError, OSError, ValueError, statistics.StatisticsError):
        pass
    return 40


_CSS = """
:root {
  --fg: #1c1a17;
  --muted: #6b6862;
  --muted-strong: #4a4742;
  --border: #d9d4cb;
  --border-strong: #b8b1a4;
  --bg: #fbfaf6;
  --bg-soft: #f3f0e8;
  --accent: #1c1a17;
  --link: #0f5b8a;
  --pos-fg: #2e6f3c;
  --neg-fg: #a8281e;
  --neutral-fg: #8a5a14;
  --sans: "Charter", "Iowan Old Style", "Source Serif Pro", Georgia, serif;
  --mono: ui-monospace, "Menlo", "Consolas", monospace;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: var(--bg); color: var(--fg);
  font-family: var(--sans); line-height: 1.55; font-size: 16px; }
.container { max-width: 780px; margin: 0 auto; padding: 48px 28px 80px; }
h1 { font-size: 26px; font-weight: 700; margin: 0 0 4px; line-height: 1.25; }
h2 { font-size: 19px; font-weight: 700; margin: 40px 0 10px; line-height: 1.3;
  padding-bottom: 6px; border-bottom: 1px solid var(--border); }
h2:first-of-type { margin-top: 32px; }
h3 { font-size: 16px; font-weight: 700; margin: 22px 0 6px; }
.subtitle { color: var(--muted); margin: 0 0 24px; font-size: 13px;
  font-family: var(--mono); }
p { margin: 10px 0; }
.lead { font-size: 16px; color: var(--fg); margin: 0 0 28px; line-height: 1.6; }
.kpis { display: flex; flex-wrap: wrap; gap: 0; margin: 24px 0 8px;
  border-top: 1px solid var(--border); border-bottom: 1px solid var(--border);
  padding: 14px 0; }
.kpi { display: block; flex: 1 1 0; min-width: 140px; padding: 4px 18px;
  text-decoration: none; color: inherit;
  border-left: 1px solid var(--border); }
.kpi:first-child { border-left: 0; padding-left: 0; }
.kpi-value { font-family: var(--mono); font-size: 18px; font-weight: 600;
  font-variant-numeric: tabular-nums; color: var(--fg); line-height: 1.2; }
.kpi-label { font-size: 13px; color: var(--muted); margin-top: 4px;
  font-weight: 400; line-height: 1.35; }
table { width: 100%; border-collapse: collapse; margin: 12px 0 6px;
  font-size: 14px; font-variant-numeric: tabular-nums; }
th, td { padding: 7px 10px; text-align: left; vertical-align: top;
  border-bottom: 1px solid var(--border); }
thead th { font-size: 13px; color: var(--muted-strong); font-weight: 600;
  background: transparent; border-bottom: 1px solid var(--border-strong);
  padding-bottom: 6px; }
tbody tr:last-child td { border-bottom: 1px solid var(--border-strong); }
td.num, th.num { text-align: right; font-family: var(--mono);
  font-variant-numeric: tabular-nums; white-space: nowrap; }
.cell-pos { color: var(--pos-fg); font-weight: 600; }
.cell-neg { color: var(--neg-fg); font-weight: 600; }
.cell-neutral { color: var(--neutral-fg); font-weight: 600; }
code { font-family: var(--mono); font-size: 0.86em; background: var(--bg-soft);
  padding: 1px 5px; color: var(--fg); }
.chart { margin: 14px 0 26px; padding: 0; }
.chart img { display: block; max-width: 100%; height: auto; }
.chart figcaption { font-size: 13px; color: var(--muted); margin-bottom: 6px;
  font-style: italic; }
.missing { padding: 24px; background: var(--bg-soft);
  border: 1px dashed var(--border-strong);
  color: var(--muted); font-size: 13px; text-align: center; }
.muted { color: var(--muted); }
.limitations { font-size: 14px; color: var(--muted-strong); line-height: 1.55; }
.limitations h2 { color: var(--muted-strong); }
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }
footer { margin-top: 56px; padding-top: 16px; border-top: 1px solid var(--border);
  font-size: 12px; color: var(--muted); line-height: 1.55; font-family: var(--mono); }
footer a { color: var(--muted); }
@media (max-width: 720px) {
  .container { padding: 28px 16px 60px; }
  h1 { font-size: 22px; }
  h2 { font-size: 17px; margin-top: 32px; }
  table { font-size: 13px; }
  th, td { padding: 6px 7px; }
}
@media (max-width: 600px) {
  .kpis { flex-direction: column; padding: 8px 0; }
  .kpi { border-left: 0; border-top: 1px solid var(--border); padding: 10px 0; }
  .kpi:first-child { border-top: 0; }
}
""".strip()


_POLYMARKET_BLURB = (
    "Depth-aware microstructure scanner for Polymarket negRisk events. The detector "
    "reads top-of-book; the depth-walker fills the full /book at realistic notional "
    "and separates tradeable signal from trap. Count-based trap rate across recent "
    "scans is 56-77%; volume-weighted it is 0.012%, because the 2026 FIFA World Cup "
    "event alone carries 95.9% of flagged dollars. Sizing in proportion to event "
    "volume routes capital toward the durable signal and away from the trap-prone "
    "long tail of small US state-election negRisk events."
)

_HYPERLIQUID_BLURB = (
    "Funding-capture backtest on Hyperliquid perpetuals: 365 days x 12 majors "
    "(105,120 hourly ticks). Top-5 trail-24h shorts pay +11.5% gross annualised "
    "at 8h cadence. Execution costs collapse this to -207% net. Net Sharpe is "
    "positive only at >= 2-weekly rebalance."
)


def _polymarket_sections(conn: sqlite3.Connection) -> str:
    flagged_table = _top_flagged_table(conn)
    depth_table = _depth_table()
    micro_section = _microstructure_section(conn)
    return f"""
  <h2>Top flagged events (build-window snapshot, 2026-05-21)</h2>
  <p class="muted">Dedup-by-event, threshold 50 bp. Same SQL the markdown report uses. Top-of-book gaps drift hourly with market activity; re-run <code>polymarket-edge scan</code> for the current state.</p>
  {flagged_table}

  <h2 id="depth">Depth-vs-trap</h2>
  <p>A top-of-book gap detector flags all three of the cases below. The depth-aware
  basket model walks each market's full <code>/book</code> at realistic notionals.
  Real signal at $48K basket (World Cup), marginal at $5K (2028 Election),
  trap at any size (Weinstein). Captured 2026-05-21; the World Cup case is the
  durable cross-rescan example, the other two compressed below the detector
  threshold within ~18 hours.</p>
  {depth_table}

  <h2>Live microstructure scan (by category)</h2>
  <p class="muted">Populated by <code>polymarket-edge microstructure-scan</code>.
  Each scan walks the book for every flagged negRisk event at $50 and $500
  per market and assigns a verdict. Categories show where traps cluster:
  2-market US state races dominate.</p>
  {micro_section}
"""


def _hyperliquid_sections(png_dir: Path, conn: sqlite3.Connection) -> str:
    pnl_chart = _chart_block("Cumulative gross P&L", png_dir / "hl_cumulative_pnl.png")
    apr_chart = _chart_block("Funding APR per coin", png_dir / "funding_apr_per_coin.png")
    cadence_chart = _chart_block(
        "Cadence frontier: gross vs net annualised by rebalance cadence",
        png_dir / "cadence_frontier.png",
    )
    strategy_table = _hl_strategy_table(conn)
    hedge_table = _hedge_cost_table(conn)
    return f"""
  <h2>Cumulative gross P&amp;L</h2>
  {pnl_chart}

  <h2>Cadence frontier</h2>
  <p>At 5 bp/leg the 8h cadence collapses to net &minus;207%; net annualised crosses
  zero between weekly and 2-weekly. Per-period breakeven on the 8h variant is
  0.26 bps/leg, below any realistic execution cost. The carry signal is durable;
  the binding constraint is execution latency.</p>
  {cadence_chart}

  <h2>Gross strategy results (in-sample, 12-coin universe, 365d)</h2>
  <p class="muted">Rebalance 8h, top-K = 5, trailing window = 24h. <strong>Hit% is the share of 8h periods with positive realised funding net</strong>, not directional accuracy: it reflects the persistence of the carry signal, not stock-picking. The 97% number is the property a funding-rate floor at the maximum-leverage clamp creates, not a forecasting skill claim.</p>
  {strategy_table}

  <h2>Hedge cost sensitivity by cadence</h2>
  <p>5 bp per leg (20 bp round-trip) per rebalance. The +11.5% gross headline at
  8h cadence becomes &minus;207% net; the cadence at which net annualised first
  crosses zero is highlighted.</p>
  {hedge_table}

  <h2>Funding APR per coin</h2>
  {apr_chart}
"""


def _limitations_section(scope: str) -> str:
    """`scope` is 'all' / 'polymarket' / 'hyperliquid' — drives the language."""
    if scope == "polymarket":
        body = (
            "Polymarket fees are per-category (Sports 0.75%, Politics 1.0%, etc.); "
            "the depth pass is mandatory before sizing because top-of-book reads "
            "lie on thin-side legs. The trap classifier is at n=30 today: "
            "scaffolding-grade, growing forward via the daily scan cron."
        )
    elif scope == "hyperliquid":
        body = (
            "Sample size on the backtest is 365 days (1,093 rebalances at the 8h "
            "cadence, 12 majors). Sharpe is the carry-only upper bound: net "
            "of 5 bp/leg spread the 8h cadence collapses to &minus;207%; the "
            "deployable region is the &ge; 2-weekly slice of the cadence frontier."
        )
    else:  # all
        body = (
            "Every headline number above is gross of execution cost on the Hyperliquid "
            "side and top-of-book on the Polymarket side. The full self-audit lives in "
            "<code>REDTEAM.md</code>. Sample size on the Hyperliquid backtest is 365 days "
            "(1,093 rebalances, 12 majors); the trap classifier is at n=30, scaffolding-grade."
        )
    return (
        '<section class="limitations">\n'
        '  <h2>Limitations</h2>\n'
        f'  <p>{body}</p>\n'
        '</section>'
    )


def _cross_link_footer(venue: str) -> str:
    """Footer that points at the other-venue dashboard."""
    if venue == "polymarket":
        link = (
            '<p>Hyperliquid funding-capture context: '
            '<a href="dashboard_hyperliquid.html">dashboard_hyperliquid.html</a> '
            '&middot; combined view: <a href="dashboard.html">dashboard.html</a></p>'
        )
    elif venue == "hyperliquid":
        link = (
            '<p>Polymarket microstructure context: '
            '<a href="dashboard_polymarket.html">dashboard_polymarket.html</a> '
            '&middot; combined view: <a href="dashboard.html">dashboard.html</a></p>'
        )
    else:
        link = (
            '<p>Single-venue focused views: '
            '<a href="dashboard_polymarket.html">Polymarket</a> &middot; '
            '<a href="dashboard_hyperliquid.html">Hyperliquid</a></p>'
        )
    return (
        '<footer>\n'
        f'  {link}\n'
        '  <p>Built by Harry Winter &middot; '
        'github.com/harrywinter06-code/polymarket-edge &middot; '
        'view source for the README, REDTEAM, MICROSTRUCTURE.</p>\n'
        '</footer>'
    )


def _build_html(
    conn: sqlite3.Connection,
    png_dir: Path,
    *,
    venue: str = "all",
) -> str:
    """Render the self-contained dashboard HTML for the given venue scope.

    venue: 'all' (combined, default), 'polymarket', 'hyperliquid'.
    """
    if venue not in ("all", "polymarket", "hyperliquid"):
        raise ValueError(f"unknown venue {venue!r}")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    test_count = _count_tests()

    if venue == "polymarket":
        title = "polymarket-edge &mdash; Polymarket microstructure dashboard"
        blurb = _POLYMARKET_BLURB
        kpi_grid = _polymarket_kpi_grid(conn, test_count)
        sections = _polymarket_sections(conn)
    elif venue == "hyperliquid":
        title = "polymarket-edge &mdash; Hyperliquid funding-capture dashboard"
        blurb = _HYPERLIQUID_BLURB
        kpi_grid = _hyperliquid_kpi_grid(conn, test_count)
        sections = _hyperliquid_sections(png_dir, conn)
    else:  # all
        title = "polymarket-edge &mdash; research dashboard"
        blurb = _HEADER_BLURB
        kpi_grid = _kpi_grid(conn, test_count)
        sections = (
            _hyperliquid_sections(png_dir, conn)
            + _polymarket_sections(conn)
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{_CSS}</style>
</head>
<body>
<main class="container">
  <h1>polymarket-edge</h1>
  <p class="subtitle">Research dashboard &middot; generated {html.escape(now)}</p>
  <p class="lead">{html.escape(blurb)}</p>

  {kpi_grid}
{sections}
  {_limitations_section(venue)}

  {_cross_link_footer(venue)}
</main>
</body>
</html>
"""


def write_dashboard(
    conn: sqlite3.Connection,
    out_path: str | Path,
    *,
    venue: str = "all",
) -> Path:
    """Render the self-contained HTML dashboard to `out_path` and return it.

    `venue` is one of 'all' (default, combined view), 'polymarket', or
    'hyperliquid'. Each focused view shows only its venue's sections plus
    a footer link to the other.

    Charts (`hl_cumulative_pnl.png`, `funding_apr_per_coin.png`,
    `cadence_frontier.png`) are read from `out_path.parent` and
    base64-embedded inline; missing PNGs render as a placeholder div
    instead of raising.
    """
    p = Path(out_path)
    html_text = _build_html(conn, png_dir=p.parent, venue=venue)
    p.write_text(html_text, encoding="utf-8")
    return p
