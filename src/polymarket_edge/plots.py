"""Chart generation for the polymarket-edge research note.

Three figures, each a standalone function:

  - `plot_hl_cumulative_pnl`: cumulative funding-capture P&L over time for
    the trailing-mean top-K strategy, overlaid with the perfect-hindsight
    ceiling and passive-BTC baseline. Marks the strategy's max drawdown
    on the cumulative curve.
  - `plot_funding_apr_per_coin`: horizontal bar chart of per-coin annualized
    funding (mean hourly funding * 24 * 365), with the base-rate floor
    drawn as a vertical reference. Bars above the floor are highlighted.
  - `plot_depth_decay`: gap-vs-basket-notional curves for one or more events,
    overlaid with each event's top-of-book gap as a dashed reference. The
    "Weinstein collapses below zero" story is the visual headline.

All functions handle the empty-data case by writing a placeholder PNG with
an explanatory text label so downstream consumers (`report.py`, `cli.py`)
can always count on an existing file at `out_path`.

Uses the Agg backend so it works headless / on CI.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections.abc import Mapping, Sequence
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.axes import Axes

from polymarket_edge.book_depth import EventDepthResult
from polymarket_edge.hl_backtest import HOURS_PER_YEAR, FundingTick, load_funding

BASE_RATE_FLOOR_APR_PCT = 10.95  # Hyperliquid base-rate funding floor, % APR

# Restrained palette modelled on Tailwind slate + accents. Indices are referenced
# explicitly (not by name) so callers stay declarative: 0/1/2 = neutrals, 3/4/5 =
# semantic (negative / positive / warning).
_PALETTE: tuple[str, ...] = (
    "#0f172a",  # slate-900 — primary line
    "#475569",  # slate-600 — secondary line / muted
    "#1e293b",  # slate-800 — emphasis
    "#dc2626",  # rose-600 — negative / drawdown
    "#059669",  # emerald-600 — positive / ceiling
    "#d97706",  # amber-600 — warning / reference
)
_GRID_COLOR = "#cbd5e1"  # slate-300
_AXIS_COLOR = "#334155"  # slate-700
_SAVE_DPI = 144

matplotlib.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 10.0,
        "axes.titlesize": 12.0,
        "axes.titleweight": "600",
        "axes.labelsize": 10.0,
        "axes.labelcolor": _AXIS_COLOR,
        "axes.edgecolor": _AXIS_COLOR,
        "axes.linewidth": 0.5,
        "xtick.color": _AXIS_COLOR,
        "ytick.color": _AXIS_COLOR,
        "xtick.labelsize": 9.0,
        "ytick.labelsize": 9.0,
        "legend.frameon": False,
        "legend.fontsize": 9.0,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "savefig.facecolor": "white",
    }
)


def _style_axes(ax: Axes, *, grid_axis: str = "y") -> None:
    """Apply the consistent despine + subtle-grid look used across all charts."""
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_linewidth(0.5)
    ax.spines["bottom"].set_linewidth(0.5)
    ax.spines["left"].set_color(_AXIS_COLOR)
    ax.spines["bottom"].set_color(_AXIS_COLOR)
    ax.grid(False)
    ax.grid(
        True, axis=grid_axis, linestyle=":", linewidth=0.7, alpha=0.4, color=_GRID_COLOR
    )
    ax.set_axisbelow(True)
    ax.tick_params(length=3, width=0.5, pad=4)


def _save_placeholder(out_path: Path, message: str) -> Path:
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.text(
        0.5,
        0.5,
        message,
        ha="center",
        va="center",
        fontsize=11,
        color=_AXIS_COLOR,
        wrap=True,
        transform=ax.transAxes,
    )
    ax.set_axis_off()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=_SAVE_DPI, bbox_inches="tight")
    plt.close(fig)
    return out_path


# ---------- (1) cumulative HL backtest P&L ----------------------------------


def _series_by_coin(ticks: Sequence[FundingTick]) -> dict[str, list[FundingTick]]:
    out: dict[str, list[FundingTick]] = {}
    for t in ticks:
        out.setdefault(t.coin, []).append(t)
    for k in out:
        out[k].sort(key=lambda x: x.t_ms)
    return out


def _common_grid(per_coin: Mapping[str, Sequence[FundingTick]]) -> list[int]:
    if not per_coin:
        return []
    sets = [{t.t_ms for t in series} for series in per_coin.values()]
    common = set.intersection(*sets) if sets else set()
    return sorted(common)


def _per_rebalance_returns_trailing(
    ticks: Sequence[FundingTick],
    *,
    top_k: int,
    trailing_hours: int,
    rebalance_hours: int,
) -> tuple[list[int], list[float]]:
    """Re-run the trailing-mean top-K loop locally to capture the per-rebalance
    return series alongside its end-of-interval timestamp. Matches the math in
    `hl_backtest.backtest_top_k_trailing` exactly."""
    per_coin = _series_by_coin(ticks)
    grid = _common_grid(per_coin)
    if len(grid) < trailing_hours + rebalance_hours:
        return [], []
    maps = {c: {t.t_ms: t.funding for t in s} for c, s in per_coin.items()}
    ts: list[int] = []
    rets: list[float] = []
    i = trailing_hours
    while i + rebalance_hours <= len(grid):
        window = grid[i - trailing_hours : i]
        trail_mean: dict[str, float] = {}
        for c, m in maps.items():
            vals = [m[t] for t in window if t in m]
            if len(vals) == trailing_hours:
                trail_mean[c] = statistics.fmean(vals)
        if not trail_mean:
            i += rebalance_hours
            continue
        top = sorted(trail_mean.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        held = [c for c, _ in top]
        future = grid[i : i + rebalance_hours]
        total_short_pnl = 0.0
        n = 0
        for c in held:
            m = maps[c]
            vals = [m[t] for t in future if t in m]
            if len(vals) == len(future):
                total_short_pnl += sum(vals)
                n += 1
        if n > 0:
            rets.append(total_short_pnl / n)
            ts.append(future[-1])
        i += rebalance_hours
    return ts, rets


def _per_rebalance_returns_perfect(
    ticks: Sequence[FundingTick],
    *,
    top_k: int,
    rebalance_hours: int,
) -> tuple[list[int], list[float]]:
    per_coin = _series_by_coin(ticks)
    grid = _common_grid(per_coin)
    maps = {c: {t.t_ms: t.funding for t in s} for c, s in per_coin.items()}
    ts: list[int] = []
    rets: list[float] = []
    i = 0
    while i + rebalance_hours <= len(grid):
        future = grid[i : i + rebalance_hours]
        realized: dict[str, float] = {}
        for c, m in maps.items():
            vals = [m[t] for t in future if t in m]
            if len(vals) == rebalance_hours:
                realized[c] = sum(vals)
        if realized:
            top = sorted(realized.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
            rets.append(sum(v for _, v in top) / len(top))
            ts.append(future[-1])
        i += rebalance_hours
    return ts, rets


def _per_rebalance_returns_passive(
    ticks: Sequence[FundingTick],
    *,
    coin: str,
    rebalance_hours: int,
) -> tuple[list[int], list[float]]:
    series = sorted([t for t in ticks if t.coin == coin], key=lambda t: t.t_ms)
    ts: list[int] = []
    rets: list[float] = []
    for i in range(0, len(series) - rebalance_hours + 1, rebalance_hours):
        chunk = series[i : i + rebalance_hours]
        if len(chunk) < rebalance_hours:
            break
        rets.append(sum(t.funding for t in chunk))
        ts.append(chunk[-1].t_ms)
    return ts, rets


def _cumulative(returns: Sequence[float]) -> list[float]:
    out: list[float] = []
    running = 0.0
    for r in returns:
        running += r
        out.append(running)
    return out


def _max_drawdown_indices(cum: Sequence[float]) -> tuple[int, int, float]:
    """Return (peak_index, trough_index, drawdown_magnitude) for the worst
    drawdown in the cumulative series. peak_index==trough_index==0 and dd==0
    if no drawdown occurred."""
    if not cum:
        return 0, 0, 0.0
    peak = cum[0]
    peak_idx = 0
    worst_dd = 0.0
    worst_peak_idx = 0
    worst_trough_idx = 0
    for i, v in enumerate(cum):
        if v > peak:
            peak = v
            peak_idx = i
        dd = peak - v
        if dd > worst_dd:
            worst_dd = dd
            worst_peak_idx = peak_idx
            worst_trough_idx = i
    return worst_peak_idx, worst_trough_idx, worst_dd


def plot_hl_cumulative_pnl(
    conn: sqlite3.Connection,
    out_path: str | Path,
    *,
    top_k: int = 5,
    trailing_hours: int = 24,
    rebalance_hours: int = 8,
) -> Path:
    """Plot cumulative funding-capture P&L for the trailing-mean top-K
    strategy versus the perfect-hindsight ceiling and passive-BTC baseline.
    The trailing strategy's max drawdown is marked on the curve."""
    out = Path(out_path)
    ticks = load_funding(conn)
    if not ticks:
        return _save_placeholder(out, "No Hyperliquid funding history present.")

    _strat_ts, strat_rets = _per_rebalance_returns_trailing(
        ticks,
        top_k=top_k,
        trailing_hours=trailing_hours,
        rebalance_hours=rebalance_hours,
    )
    if not strat_rets:
        return _save_placeholder(
            out,
            f"Not enough funding history for trailing={trailing_hours}h "
            f"+ rebalance={rebalance_hours}h backtest.",
        )

    _perf_ts, perf_rets = _per_rebalance_returns_perfect(
        ticks, top_k=top_k, rebalance_hours=rebalance_hours
    )
    btc_ticks = [t for t in ticks if t.coin == "BTC"]
    passive_rets: list[float] = []
    if btc_ticks:
        _passive_ts, passive_rets = _per_rebalance_returns_passive(
            ticks, coin="BTC", rebalance_hours=rebalance_hours
        )

    strat_cum = _cumulative(strat_rets)
    perf_cum = _cumulative(perf_rets)
    passive_cum = _cumulative(passive_rets)

    fig, ax = plt.subplots(figsize=(10, 5))
    x_strat = list(range(1, len(strat_cum) + 1))
    ax.plot(
        x_strat,
        [v * 100 for v in strat_cum],
        label=f"trailing-{trailing_hours}h top-{top_k} (strategy)",
        color=_PALETTE[0],
        linewidth=2.0,
    )
    if perf_cum:
        x_perf = list(range(1, len(perf_cum) + 1))
        ax.plot(
            x_perf,
            [v * 100 for v in perf_cum],
            label="perfect hindsight (ceiling)",
            color=_PALETTE[4],
            linewidth=1.4,
            linestyle="--",
        )
    if passive_cum:
        x_pass = list(range(1, len(passive_cum) + 1))
        ax.plot(
            x_pass,
            [v * 100 for v in passive_cum],
            label="passive short BTC",
            color=_PALETTE[1],
            linewidth=1.4,
            linestyle=":",
        )

    peak_i, trough_i, dd_mag = _max_drawdown_indices(strat_cum)
    if dd_mag > 0:
        ax.plot(
            [peak_i + 1, trough_i + 1],
            [strat_cum[peak_i] * 100, strat_cum[trough_i] * 100],
            color=_PALETTE[3],
            linewidth=2.0,
            marker="o",
            markersize=5,
            label=f"max drawdown ({dd_mag * 100:.2f}pp)",
        )

    ax.axhline(0.0, color=_AXIS_COLOR, linewidth=0.5)
    ax.set_xlabel(f"rebalance tick (every {rebalance_hours}h)", labelpad=8)
    ax.set_ylabel("cumulative short-funding return (%)", labelpad=8)
    ax.set_title(
        f"hyperliquid funding-capture cumulative p&l  "
        f"top-{top_k}, trailing {trailing_hours}h, rebalance {rebalance_hours}h",
        loc="left",
        pad=12,
    )
    _style_axes(ax, grid_axis="y")
    ax.legend(loc="best")
    fig.tight_layout(pad=1.2)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=_SAVE_DPI, bbox_inches="tight")
    plt.close(fig)
    return out


# ---------- (2) per-coin annualized funding ---------------------------------


def plot_funding_apr_per_coin(
    conn: sqlite3.Connection,
    out_path: str | Path,
    *,
    top_n: int = 20,
) -> Path:
    """Horizontal bar chart of per-coin annualized funding rates, sorted
    descending. The base-rate floor (~10.95% APR) is drawn as a vertical
    reference and bars above the floor are highlighted in a distinct color."""
    out = Path(out_path)
    rows = conn.execute(
        "SELECT coin, funding FROM hl_funding_history ORDER BY coin, t"
    ).fetchall()
    if not rows:
        return _save_placeholder(out, "No Hyperliquid funding history present.")

    by_coin: dict[str, list[float]] = {}
    for r in rows:
        by_coin.setdefault(str(r[0]), []).append(float(r[1]))

    coin_stats: list[tuple[str, float, float]] = []
    for c, vs in by_coin.items():
        if len(vs) < 2:
            continue
        apr = statistics.fmean(vs) * HOURS_PER_YEAR * 100.0
        vol = statistics.pstdev(vs) * (HOURS_PER_YEAR ** 0.5) * 100.0
        coin_stats.append((c, apr, vol))

    if not coin_stats:
        return _save_placeholder(out, "Insufficient per-coin funding samples (need >=2).")

    coin_stats.sort(key=lambda x: x[1], reverse=True)
    top = coin_stats[:top_n]
    coins = [c for c, _, _ in top]
    aprs = [apr for _, apr, _ in top]
    above = [apr > BASE_RATE_FLOOR_APR_PCT for apr in aprs]
    # Above-floor coins: emerald (positive signal); at-or-below: slate (neutral).
    colors = [_PALETTE[4] if hi else _PALETTE[1] for hi in above]

    fig, ax = plt.subplots(figsize=(9, max(4.0, 0.32 * len(coins) + 1.5)))
    y = list(range(len(coins)))
    ax.barh(y, aprs, color=colors, edgecolor="none", height=0.6)
    ax.set_yticks(y)
    ax.set_yticklabels(coins)
    ax.invert_yaxis()
    ax.axvline(
        BASE_RATE_FLOOR_APR_PCT,
        color=_PALETTE[3],
        linewidth=1.0,
        linestyle="--",
        label=f"base-rate floor ({BASE_RATE_FLOOR_APR_PCT:.2f}% apr)",
    )
    ax.axvline(0.0, color=_AXIS_COLOR, linewidth=0.5)
    ax.set_xlabel("annualized mean funding (% apr, hourly mean * 24 * 365)", labelpad=8)
    ax.set_title(
        f"per-coin annualized funding  top {len(coins)} by mean realized rate",
        loc="left",
        pad=12,
    )
    _style_axes(ax, grid_axis="x")
    ax.legend(loc="lower right")
    fig.tight_layout(pad=1.2)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=_SAVE_DPI, bbox_inches="tight")
    plt.close(fig)
    return out


# ---------- (3) depth-decay curves ------------------------------------------


def plot_depth_decay(
    depth_results_by_event_name: Mapping[str, Sequence[EventDepthResult]],
    out_path: str | Path,
) -> Path:
    """Plot `gap_depth_aware` vs `notional_per_market_usd` (log x) for each
    event. Overlay each event's top-of-book gap as a flat dashed reference.
    The throttle market at each event's terminal point is annotated. A
    horizontal zero line makes the "gap collapses below zero" finding obvious.

    `depth_results_by_event_name` maps event_name -> list of EventDepthResult,
    each list expected to be sorted by `notional_per_market_usd` ascending."""
    out = Path(out_path)
    non_empty = {k: list(v) for k, v in depth_results_by_event_name.items() if v}
    if not non_empty:
        return _save_placeholder(out, "No depth-probe results to plot.")

    fig, ax = plt.subplots(figsize=(11, 6))
    # Cycle the restrained palette starting from primary slate -> emphasis ->
    # rose (for the "trap" event, when present).
    line_colors = (_PALETTE[0], _PALETTE[3], _PALETTE[4], _PALETTE[2], _PALETTE[5])

    for idx, (name, results) in enumerate(non_empty.items()):
        color = line_colors[idx % len(line_colors)]
        xs = [r.notional_per_market_usd for r in results]
        ys = [r.gap_depth_aware * 10_000 for r in results]  # convert to bps
        ax.plot(
            xs,
            ys,
            marker="o",
            markersize=4,
            color=color,
            linewidth=1.8,
            label=f"{name} (depth-aware)",
        )
        top_gap_bps = results[0].gap_top_of_book * 10_000
        ax.axhline(
            top_gap_bps,
            color=color,
            linewidth=0.8,
            linestyle="--",
            alpha=0.45,
            label=f"{name} (top-of-book = {top_gap_bps:.0f}bps)",
        )
        # Annotate the throttle market at the most-stressed (final) point.
        last = results[-1]
        if last.basket_throttle_market:
            label = last.basket_throttle_market
            if len(label) > 40:
                label = label[:37] + "..."
            ax.annotate(
                f"throttle: {label}",
                xy=(last.notional_per_market_usd, last.gap_depth_aware * 10_000),
                xytext=(8, 6),
                textcoords="offset points",
                fontsize=8,
                color=color,
                arrowprops={"arrowstyle": "->", "color": color, "alpha": 0.5, "lw": 0.6},
            )

    ax.axhline(0.0, color=_AXIS_COLOR, linewidth=0.5)
    ax.set_xscale("log")
    ax.set_xlabel("basket notional per market (usd, log scale)", labelpad=8)
    ax.set_ylabel("event-level gap (bps)", labelpad=8)
    ax.set_title(
        "depth-aware arb gap vs basket size  top-of-book is a mirage at scale",
        loc="left",
        pad=12,
    )
    _style_axes(ax, grid_axis="y")
    ax.legend(loc="best", fontsize=8, ncol=1)
    fig.tight_layout(pad=1.2)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=_SAVE_DPI, bbox_inches="tight")
    plt.close(fig)
    return out
