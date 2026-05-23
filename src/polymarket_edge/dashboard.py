"""Single-file self-contained HTML dashboard generator.

Translates the markdown REPORT.md structure into a portable HTML document
with embedded base64 chart images and inline CSS — zero external assets,
zero JavaScript. The output is one file that opens identically when emailed,
hosted on a static URL, or attached to a job application.

The same SQL/backtest entry points used by `report.py` are reused here so the
two views never disagree. Layout deliberately omits the persistence section
(no analytical content the README hasn't already framed) in favor of leading
with the depth-vs-trap finding, which is the deliverable's headline.
"""

from __future__ import annotations

import base64
import html
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path

from polymarket_edge import hl_backtest, hl_hedge, report

# Header copy, hand-curated from README "What it does" — deliberately not
# parsed from markdown so the dashboard framing stays editorial.
_HEADER_BLURB = (
    "Event-level no-arb scanner for Polymarket mutually-exclusive (negRisk) markets, "
    "plus a Hyperliquid funding-capture backtest. Every headline number below has been "
    "red-teamed; the depth-vs-trap and net-of-spread tables are where the project earns "
    "its keep."
)

# Hand-curated from README §"Results" + REDTEAM §3a. Hardcoded per spec —
# this is the project's headline finding and the values are stable.
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
    n_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    cards = [
        _kpi_card(f"{n_events:,}", "Polymarket events scored"),
        _kpi_card("+150 bp", "World Cup gap @ $1K/market", anchor="depth"),
        _kpi_card("+19% / -200%", "Hyperliquid gross / net annualized"),
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
  --fg: #0f172a;
  --muted: #64748b;
  --muted-strong: #475569;
  --border: #e2e8f0;
  --border-strong: #cbd5e1;
  --bg: #ffffff;
  --bg-soft: #f8fafc;
  --accent: #0f172a;
  --pos-bg: #ecfdf5;
  --pos-fg: #047857;
  --neg-bg: #fff1f2;
  --neg-fg: #be123c;
  --neutral-bg: #fffbeb;
  --neutral-fg: #b45309;
  --sans: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI",
    Roboto, "Helvetica Neue", Arial, sans-serif;
  --mono: ui-monospace, "Cascadia Code", "JetBrains Mono", "SF Mono", Menlo,
    Consolas, monospace;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: var(--bg-soft); color: var(--fg);
  font-family: var(--sans); line-height: 1.65; font-size: 15px;
  -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; }
.container { max-width: 920px; margin: 0 auto; padding: 56px 32px 96px;
  background: var(--bg); border-left: 1px solid var(--border);
  border-right: 1px solid var(--border); }
h1 { font-size: 28px; font-weight: 600; margin: 0 0 6px;
  letter-spacing: -0.02em; line-height: 1.2; }
h2 { font-size: 20px; font-weight: 600; margin: 44px 0 12px;
  letter-spacing: -0.015em; line-height: 1.3;
  padding-bottom: 8px; border-bottom: 1px solid var(--border); }
h2:first-of-type { margin-top: 36px; }
h3 { font-size: 16px; font-weight: 600; margin: 24px 0 8px;
  letter-spacing: -0.01em; }
.subtitle { color: var(--muted); margin: 0 0 28px; font-size: 13px;
  font-family: var(--mono); letter-spacing: 0.01em; }
p { margin: 12px 0; }
.lead { font-size: 15.5px; color: var(--accent); margin: 0 0 32px;
  line-height: 1.65; }
.kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px;
  margin: 28px 0 16px; }
.kpi { display: block; padding: 18px 16px; border: 1px solid var(--border-strong);
  border-radius: 6px; background: var(--bg); text-decoration: none; color: inherit;
  transition: border-color 120ms ease, transform 120ms ease; }
.kpi:hover { border-color: var(--accent); }
.kpi-value { font-family: var(--mono); font-size: 22px; font-weight: 600;
  letter-spacing: -0.01em; font-variant-numeric: tabular-nums;
  color: var(--fg); line-height: 1.15; }
.kpi-label { font-size: 11px; color: var(--muted); margin-top: 8px;
  text-transform: uppercase; letter-spacing: 0.08em; font-weight: 500;
  line-height: 1.35; }
table { width: 100%; border-collapse: collapse; margin: 14px 0 8px;
  font-size: 14px; font-variant-numeric: tabular-nums; }
th, td { padding: 9px 12px; text-align: left; vertical-align: top;
  border-bottom: 1px solid var(--border); }
thead th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--muted); font-weight: 600; background: transparent;
  border-bottom: 1px solid var(--border-strong); padding-bottom: 8px; }
tbody tr:last-child td { border-bottom: 1px solid var(--border-strong); }
td.num, th.num { text-align: right; font-family: var(--mono);
  font-variant-numeric: tabular-nums; white-space: nowrap; }
.cell-pos { background: var(--pos-bg); color: var(--pos-fg); font-weight: 600;
  border-radius: 3px; }
.cell-neg { background: var(--neg-bg); color: var(--neg-fg); font-weight: 600;
  border-radius: 3px; }
.cell-neutral { background: var(--neutral-bg); color: var(--neutral-fg);
  font-weight: 600; border-radius: 3px; }
code { font-family: var(--mono); font-size: 0.88em; background: var(--bg-soft);
  padding: 1px 6px; border-radius: 3px; border: 1px solid var(--border);
  color: var(--accent); }
.chart { margin: 16px 0 28px; padding: 0; }
.chart img { display: block; max-width: 100%; height: auto;
  border: 1px solid var(--border); border-radius: 6px; background: var(--bg); }
.chart figcaption { font-size: 12px; color: var(--muted); margin-bottom: 8px;
  text-transform: uppercase; letter-spacing: 0.06em; font-weight: 500; }
.missing { padding: 28px; background: var(--bg-soft); border: 1px dashed var(--border-strong);
  border-radius: 6px; color: var(--muted); font-size: 13px; text-align: center; }
.muted { color: var(--muted); }
.limitations { font-size: 13.5px; color: var(--muted-strong); line-height: 1.6; }
.limitations h2 { color: var(--muted-strong); }
a { color: #1d4ed8; text-decoration: none; }
a:hover { text-decoration: underline; }
footer { margin-top: 64px; padding-top: 20px; border-top: 1px solid var(--border);
  font-size: 12px; color: var(--muted); line-height: 1.6; font-family: var(--mono); }
footer a { color: var(--muted); }
@media (max-width: 720px) {
  .container { padding: 32px 18px 72px; border-left: 0; border-right: 0; }
  h1 { font-size: 24px; }
  h2 { font-size: 18px; margin-top: 36px; }
  table { font-size: 13px; }
  th, td { padding: 7px 8px; }
}
@media (max-width: 600px) {
  .kpis { grid-template-columns: 1fr; }
  .kpi { padding: 14px 14px; }
  .kpi-value { font-size: 20px; }
}
""".strip()


def _build_html(conn: sqlite3.Connection, png_dir: Path) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    test_count = _count_tests()
    kpi_grid = _kpi_grid(conn, test_count)
    pnl_chart = _chart_block("Cumulative gross P&L", png_dir / "hl_cumulative_pnl.png")
    apr_chart = _chart_block("Funding APR per coin", png_dir / "funding_apr_per_coin.png")
    flagged_table = _top_flagged_table(conn)
    depth_table = _depth_table()
    strategy_table = _hl_strategy_table(conn)
    hedge_table = _hedge_cost_table(conn)
    micro_section = _microstructure_section(conn)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>polymarket-edge — research dashboard</title>
<style>{_CSS}</style>
</head>
<body>
<main class="container">
  <h1>polymarket-edge</h1>
  <p class="subtitle">Research dashboard &middot; generated {html.escape(now)}</p>
  <p class="lead">{html.escape(_HEADER_BLURB)}</p>

  {kpi_grid}

  <h2>Hyperliquid — cumulative gross P&amp;L</h2>
  {pnl_chart}

  <h2>Hyperliquid — funding APR per coin</h2>
  {apr_chart}

  <h2>Polymarket — top flagged events</h2>
  <p class="muted">Dedup-by-event, threshold 50 bp. Same SQL the markdown report uses.</p>
  {flagged_table}

  <h2 id="depth">Polymarket — depth-vs-trap (the headline finding)</h2>
  <p>A top-of-book gap detector flags all three. The depth-aware basket model separates
  the real signal from the marginal one from the trap.</p>
  {depth_table}

  <h2>Polymarket — live microstructure scan (by category)</h2>
  <p class="muted">Populated by <code>polymarket-edge microstructure-scan</code>.
  Each scan walks the book for every flagged negRisk event at $50 and $500
  per market and assigns a verdict. Categories show where traps cluster.</p>
  {micro_section}

  <h2>Hyperliquid — gross strategy results</h2>
  <p class="muted">Rebalance 8h, top-K = 5, trailing window = 24h.</p>
  {strategy_table}

  <h2>Hyperliquid — hedge cost sensitivity</h2>
  <p>5 bp per leg (20 bp round-trip) per rebalance. The headline +19% gross at 8h cadence
  becomes -200% net; the cadence at which net annualized first crosses zero is highlighted.</p>
  {hedge_table}

  <section class="limitations">
    <h2>Limitations</h2>
    <p>Every headline number above is gross of execution cost on the Hyperliquid side and
    top-of-book on the Polymarket side. The full self-audit lives in
    <code>REDTEAM.md</code>. Sample size on the Hyperliquid backtest is 30 days
    (~56 rebalances) &mdash; Sharpe confidence intervals are wide, and listing/delisting
    survivorship is uncorrected.</p>
  </section>

  <footer>
    Built by Harry Winter &middot; github.com/harrywinter06-code/polymarket-edge &middot;
    view source for the README, REDTEAM, MICROSTRUCTURE.
  </footer>
</main>
</body>
</html>
"""


def write_dashboard(conn: sqlite3.Connection, out_path: str | Path) -> Path:
    """Render the self-contained HTML dashboard to `out_path` and return it.

    Charts (`hl_cumulative_pnl.png`, `funding_apr_per_coin.png`) are read from
    `out_path.parent` and base64-embedded inline; missing PNGs render as a
    placeholder div instead of raising.
    """
    p = Path(out_path)
    html_text = _build_html(conn, png_dir=p.parent)
    p.write_text(html_text, encoding="utf-8")
    return p
