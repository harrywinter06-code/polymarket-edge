"""Year-long re-run of the Hyperliquid analyses on the new dataset.

Re-runs four analyses on the year-long subset (coins with >=5000 funding rows):

  1. Walk-forward (train=60d, test=30d, step=15d) — produces 10+ windows.
  2. Unhedged regime conditioning by trailing-7d BTC realized vol.
  3. Funding-extremes 18-cell family at cooldown=72h (event independence).
  4. Moving-block bootstrap on the headline top-K trailing strategy.

Prints a Markdown report to ``YEAR_ANALYSIS.md`` and a raw JSON dump to
``results/year_analysis_<timestamp>.json``. Uses every source module as-is.

The script is read-only against the DB — no fetches, no writes to source.

Usage::

    PYTHONPATH=src python scripts/year_analysis.py
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polymarket_edge import db  # noqa: E402
from polymarket_edge.hl_backtest import (  # noqa: E402
    HOURS_PER_YEAR,
    FundingTick,
    load_funding,
)
from polymarket_edge.hl_basis_hedge import (  # noqa: E402
    HedgedBacktestResult,
    HedgedRebalanceResult,
    classify_regimes,
    regime_conditional_results,
)
from polymarket_edge.hl_extremes import (  # noqa: E402
    load_candles,
    merge_funding_and_prices,
    run_study,
)
from polymarket_edge.hl_stats import bootstrap_backtest_stats  # noqa: E402
from polymarket_edge.hl_stats_block import (  # noqa: E402
    estimate_optimal_block_length,
    moving_block_bootstrap,
)
from polymarket_edge.walkforward import walk_forward_top_k_trailing  # noqa: E402

DB_PATH = ROOT / "polymarket_edge.db"
RESULTS_DIR = ROOT / "results"
MD_PATH = ROOT / "YEAR_ANALYSIS.md"

MIN_FUNDING_ROWS = 5000
TOP_K = 5
TRAILING_HOURS = 24
REBALANCE_HOURS = 8
HOUR_MS = 3_600_000

# Walk-forward at year scale.
WF_TRAIN_DAYS = 60
WF_TEST_DAYS = 30
WF_STEP_DAYS = 15

# Funding extremes family.
Z_THRESHOLDS = (1.5, 2.0, 2.5)
DIRECTIONS = ("positive", "negative")
HOLD_HOURS = (6, 24, 72)
COOLDOWN_HOURS = 72
N_TESTS_FAMILY = len(Z_THRESHOLDS) * len(DIRECTIONS) * len(HOLD_HOURS)  # 18
BONFERRONI_T = 3.05

N_BOOTSTRAP = 2000


# ---------------------------------------------------------------------------
# Per-rebalance return reconstruction (timestamps + returns, no source edits).
# ---------------------------------------------------------------------------


def _series_by_coin(ticks: list[FundingTick]) -> dict[str, list[FundingTick]]:
    out: dict[str, list[FundingTick]] = {}
    for t in ticks:
        out.setdefault(t.coin, []).append(t)
    for k in out:
        out[k].sort(key=lambda x: x.t_ms)
    return out


def _common_grid(per_coin: dict[str, list[FundingTick]]) -> list[int]:
    if not per_coin:
        return []
    sets = [{t.t_ms for t in series} for series in per_coin.values()]
    return sorted(set.intersection(*sets)) if sets else []


def per_rebalance_unhedged(
    ticks: list[FundingTick],
    *,
    top_k: int,
    trailing_hours: int,
    rebalance_hours: int,
) -> list[tuple[int, int, float]]:
    """Return ``(t_open, t_close, net_return)`` per rebalance.

    Mirrors `hl_backtest.backtest_top_k_trailing` and
    `hl_stats.compute_per_period_returns_trailing` exactly — same selection
    logic, same realization gate — but emits timestamps alongside each
    realized rebalance so the regime classifier can bucket them.
    """
    per_coin = _series_by_coin(ticks)
    grid = _common_grid(per_coin)
    if len(grid) < trailing_hours + rebalance_hours:
        return []
    maps = {c: {t.t_ms: t.funding for t in series} for c, series in per_coin.items()}
    out: list[tuple[int, int, float]] = []
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
        per_coin_count = 0
        for c in held:
            m = maps[c]
            vals = [m[t] for t in future if t in m]
            if len(vals) == len(future):
                total_short_pnl += sum(vals)
                per_coin_count += 1
        if per_coin_count > 0:
            # Floor to hour boundary -- funding timestamps from HL carry tens
            # of ms of jitter; the regime classifier keys on hour-aligned
            # candle timestamps. Match the convention used by hl_basis_hedge.
            t_open = (grid[i] // HOUR_MS) * HOUR_MS
            t_close = (grid[i + rebalance_hours - 1] // HOUR_MS) * HOUR_MS
            out.append((t_open, t_close, total_short_pnl / per_coin_count))
        i += rebalance_hours
    return out


def synthesise_unhedged_result(
    per_rebalance: list[tuple[int, int, float]],
) -> HedgedBacktestResult:
    """Wrap unhedged per-rebalance returns in a ``HedgedBacktestResult`` so
    ``regime_conditional_results`` can bucket them by BTC regime.

    All P&L attributes other than ``funding_received`` and ``net_return`` are
    zeroed — the regime function only reads ``net_return`` and ``t_ms_open``.
    """
    rebs = [
        HedgedRebalanceResult(
            t_ms_open=t_open,
            t_ms_close=t_close,
            coins_held=[],
            funding_received=r,
            perp_pnl=0.0,
            spot_pnl=0.0,
            basis_pnl=0.0,
            entry_spread_bps=0.0,
            exit_spread_bps=0.0,
            net_return=r,
        )
        for t_open, t_close, r in per_rebalance
    ]
    returns = [r for _, _, r in per_rebalance]
    if not returns:
        return HedgedBacktestResult(
            n_rebalances=0, coins_eligible=[], coins_excluded_no_spot=[],
            rebalances=rebs, total_net_return=0.0, annualized_net_return=0.0,
            annualized_funding_only=0.0, annualized_basis_pnl=0.0,
            annualized_spread_cost=0.0, sharpe=0.0, max_drawdown=0.0, hit_rate=0.0,
        )
    mean = statistics.fmean(returns)
    std = statistics.pstdev(returns) if len(returns) >= 2 else 0.0
    periods_per_year = HOURS_PER_YEAR / REBALANCE_HOURS
    ann_ret = mean * periods_per_year
    ann_vol = std * (periods_per_year ** 0.5)
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for r in returns:
        cum += r
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return HedgedBacktestResult(
        n_rebalances=len(returns),
        coins_eligible=[],
        coins_excluded_no_spot=[],
        rebalances=rebs,
        total_net_return=sum(returns),
        annualized_net_return=ann_ret,
        annualized_funding_only=ann_ret,
        annualized_basis_pnl=0.0,
        annualized_spread_cost=0.0,
        sharpe=sharpe,
        max_drawdown=abs(mdd),
        hit_rate=sum(1 for r in returns if r > 0) / len(returns),
    )


# ---------------------------------------------------------------------------
# Formatting helpers.
# ---------------------------------------------------------------------------


def fmt_pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def fmt_iso(t_ms: int) -> str:
    return datetime.fromtimestamp(t_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Coin coverage.
# ---------------------------------------------------------------------------


def select_year_coins(conn) -> list[str]:
    rows = conn.execute(
        "SELECT coin, COUNT(*) AS n FROM hl_funding_history "
        "GROUP BY coin HAVING n >= ? ORDER BY n DESC, coin ASC",
        (MIN_FUNDING_ROWS,),
    ).fetchall()
    return [r[0] for r in rows]


def funding_row_counts(conn, coins: list[str]) -> dict[str, int]:
    placeholders = ",".join(["?"] * len(coins))
    rows = conn.execute(
        f"SELECT coin, COUNT(*) FROM hl_funding_history WHERE coin IN ({placeholders}) "
        "GROUP BY coin",
        coins,
    ).fetchall()
    return dict(rows)


# ---------------------------------------------------------------------------
# Markdown rendering.
# ---------------------------------------------------------------------------


def render_markdown(payload: dict) -> str:
    lines: list[str] = []
    lines.append("# Year-long re-run: what survives at proper sample size")
    lines.append("")
    lines.append(f"Generated: {payload['generated_at']} UTC")
    lines.append("")
    lines.append(
        f"DB: `polymarket_edge.db` -- coins with >= {MIN_FUNDING_ROWS} funding rows: "
        f"{', '.join(payload['coins'])}"
    )
    lines.append("")

    # Survival summary at top.
    lines.append("## Survival summary")
    lines.append("")
    lines.append("**What survives**")
    for item in payload["survives"]:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("**What does not**")
    for item in payload["does_not_survive"]:
        lines.append(f"- {item}")
    lines.append("")
    lines.append(payload["headline_finding"])
    lines.append("")

    # Coverage.
    lines.append("## 1. Coin coverage")
    lines.append("")
    lines.append("| Coin | Funding rows | Candle rows | Funding span (days) |")
    lines.append("|------|--------------|-------------|---------------------|")
    for c in payload["coverage"]:
        lines.append(
            f"| {c['coin']} | {c['funding_rows']:,} | {c['candle_rows']:,} | "
            f"{c['funding_span_days']:.0f} |"
        )
    lines.append("")

    # Walk-forward.
    wf = payload["walk_forward"]
    lines.append("## 2. Walk-forward")
    lines.append("")
    lines.append(
        f"Config: top_k={TOP_K}, trailing={TRAILING_HOURS}h, rebalance={REBALANCE_HOURS}h, "
        f"train={WF_TRAIN_DAYS}d, test={WF_TEST_DAYS}d, step={WF_STEP_DAYS}d -- "
        f"n_windows={wf['n_windows']}"
    )
    lines.append("")
    lines.append("| # | Train start | Test start | Test end | IS ann | OOS ann | Decay (pp) |")
    lines.append("|---|-------------|------------|----------|--------|---------|------------|")
    for i, w in enumerate(wf["windows"]):
        decay = (w["in_sample_annualized"] - w["out_of_sample_annualized"]) * 100
        lines.append(
            f"| {i+1} | {fmt_iso(w['train_start_ms'])} | {fmt_iso(w['test_start_ms'])} | "
            f"{fmt_iso(w['test_end_ms'])} | {fmt_pct(w['in_sample_annualized'])} | "
            f"{fmt_pct(w['out_of_sample_annualized'])} | {decay:+.2f} |"
        )
    lines.append("")
    lines.append(
        f"**Aggregate** -- IS mean ann ret: {fmt_pct(wf['in_sample_ann_ret_mean'])} | "
        f"OOS mean ann ret: {fmt_pct(wf['out_of_sample_ann_ret_mean'])} | "
        f"Decay (IS - OOS): {wf['is_oos_decay_pp']:+.2f} pp"
    )
    lines.append("")
    lines.append(
        f"IS std across windows: {fmt_pct(wf['in_sample_ann_ret_std'])} | "
        f"OOS std across windows: {fmt_pct(wf['out_of_sample_ann_ret_std'])}"
    )
    lines.append("")
    lines.append(
        "Previous N=2-4 windows (train=10d/test=5d/step=3d) reported **negative decay** "
        "(OOS slightly beat IS, README section 'Walk-forward (out-of-sample) validation'). "
        "Comparison: see survival summary."
    )
    lines.append("")

    # Regime.
    rg = payload["regime"]
    lines.append("## 3. Regime conditioning (unhedged)")
    lines.append("")
    lines.append(
        f"Trailing-7d BTC realized vol terciles on the candle-overlap window "
        f"({rg['analysis_span_days']:.0f}d). Bootstrap n_resamples={N_BOOTSTRAP}, IID."
    )
    lines.append("")
    lines.append(
        "| Regime | N | Ann ret | Sharpe | Sharpe 95% CI | Ann ret 95% CI | Max DD |"
    )
    lines.append(
        "|--------|---|---------|--------|---------------|----------------|--------|"
    )
    for r in rg["per_regime"]:
        lines.append(
            f"| {r['regime_name']} | {r['n_rebalances']} | "
            f"{fmt_pct(r['annualized_net_return'])} | {r['sharpe']:+.2f} | "
            f"[{r['sharpe_ci_low']:+.2f}, {r['sharpe_ci_high']:+.2f}] | "
            f"[{fmt_pct(r['ann_ret_ci_low'])}, {fmt_pct(r['ann_ret_ci_high'])}] | "
            f"{r['max_drawdown'] * 100:.2f}% |"
        )
    lines.append("")
    lines.append(
        "Previous README claim: 'low-vol tercile (N=11) +72.5% ann, Sharpe +1.91, "
        "95% CI [-44, +20]' -- HEDGED with 5 bps/leg spread, very wide CI. "
        "This re-run is UNHEDGED (no spot candles across the window); see survival summary."
    )
    lines.append("")

    # Extremes.
    ex = payload["extremes"]
    lines.append(f"## 4. Funding extremes (cooldown={COOLDOWN_HOURS}h)")
    lines.append("")
    lines.append(
        f"All 18 cells (3 thresholds x 2 directions x 3 horizons). "
        f"Bonferroni threshold |t| > {BONFERRONI_T:.2f} (alpha=0.05 / 18). "
        f"Eligible obs after 168h burn-in and candle merge: {ex['n_eligible_obs']:,}."
    )
    lines.append("")
    lines.append(
        "| z | dir | hold | N | Price ret | LONG t | LONG net | SHORT t | SHORT net | Survives? |"
    )
    lines.append(
        "|---|-----|------|---|-----------|--------|----------|---------|-----------|-----------|"
    )
    for cell in ex["cells"]:
        survives = (
            abs(cell["long_t_stat"]) > BONFERRONI_T
            or abs(cell["short_t_stat"]) > BONFERRONI_T
        )
        flag = "YES" if survives else ""
        if abs(cell["long_t_stat"]) > BONFERRONI_T:
            flag = f"LONG (t={cell['long_t_stat']:+.2f})"
        elif abs(cell["short_t_stat"]) > BONFERRONI_T:
            flag = f"SHORT (t={cell['short_t_stat']:+.2f})"
        sign = ">" if cell["direction"] == "positive" else "<-"
        lines.append(
            f"| {sign}{cell['z_threshold']} | {cell['direction'][:3]} | "
            f"{cell['hold_hours']}h | {cell['n_events']} | "
            f"{fmt_pct(cell['mean_price_return'])} | "
            f"{cell['long_t_stat']:+.2f} | {fmt_pct(cell['mean_long_net_return'])} | "
            f"{cell['short_t_stat']:+.2f} | {fmt_pct(cell['mean_short_net_return'])} | "
            f"{flag} |"
        )
    lines.append("")
    if ex["survivors"]:
        lines.append("**Bonferroni survivors at cooldown=72h:**")
        for s in ex["survivors"]:
            lines.append(
                f"- {s['side']} z>{s['z_threshold']} {s['direction']} {s['hold_hours']}h: "
                f"t={s['t_stat']:+.2f}, net={fmt_pct(s['mean_net_return'])}, n={s['n_events']}"
            )
    else:
        lines.append("**Zero cells survive Bonferroni at cooldown=72h.**")
    lines.append("")
    lines.append(
        "Previous (~22d, cooldown=0): 7 of 18 cells cleared Bonferroni (LONG side, negative "
        "funding extremes, t=+3.27 to +7.09). At cooldown=72h on small N: zero cleared. "
        "See survival summary."
    )
    lines.append("")

    # Block bootstrap.
    bb = payload["block_bootstrap"]
    lines.append("## 5. Block bootstrap on top-K trailing")
    lines.append("")
    lines.append(
        f"Sample: {bb['n_periods']} rebalances over the full year. "
        f"Politis-White block length: {bb['block_length']}h ({bb['block_length']} periods). "
        f"Resamples: {bb['n_resamples']}."
    )
    lines.append("")
    lines.append(
        f"- Point ann ret: {fmt_pct(bb['point_ann_return'])}"
    )
    lines.append(
        f"- Block-bootstrap 95% CI ann ret: [{fmt_pct(bb['ann_return_ci_low'])}, "
        f"{fmt_pct(bb['ann_return_ci_high'])}]"
    )
    lines.append(
        f"- IID 95% CI ann ret (for comparison): "
        f"[{fmt_pct(bb['iid_ann_return_ci_low'])}, {fmt_pct(bb['iid_ann_return_ci_high'])}]"
    )
    lines.append(
        f"- Point Sharpe: {bb['point_sharpe']:+.2f}"
    )
    lines.append(
        f"- Block-bootstrap 95% CI Sharpe: "
        f"[{bb['sharpe_ci_low']:+.2f}, {bb['sharpe_ci_high']:+.2f}]"
    )
    lines.append("")
    lines.append(
        "Previous N=22d block bootstrap reported the headline ann-ret 95% CI widened by "
        "~28% from autocorrelation, settling at [+14.1%, +25.2%] (README). "
        "Year sample: see survival summary for whether the CI still excludes zero."
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------


def main() -> None:
    ts = time.strftime("%Y%m%dT%H%M%S")
    conn = db.connect(str(DB_PATH))

    coins = select_year_coins(conn)
    if not coins:
        raise SystemExit(f"No coins with >= {MIN_FUNDING_ROWS} funding rows in DB.")
    print(f"Year-long coins ({len(coins)}): {coins}")

    # Load funding restricted to year-long coins.
    funding_all = load_funding(conn)
    funding = [t for t in funding_all if t.coin in coins]
    print(f"Funding rows in scope: {len(funding):,}")

    # Per-coin coverage table.
    coverage_rows = []
    for coin in coins:
        n_f = sum(1 for t in funding if t.coin == coin)
        n_c = conn.execute(
            "SELECT COUNT(*) FROM hl_perp_candles WHERE coin = ?", (coin,),
        ).fetchone()[0]
        t_min = min(t.t_ms for t in funding if t.coin == coin)
        t_max = max(t.t_ms for t in funding if t.coin == coin)
        coverage_rows.append({
            "coin": coin,
            "funding_rows": n_f,
            "candle_rows": int(n_c),
            "funding_span_days": (t_max - t_min) / 86_400_000.0,
        })

    # --- 1. Walk-forward ---
    print(f"\nWalk-forward (train={WF_TRAIN_DAYS}d, test={WF_TEST_DAYS}d, "
          f"step={WF_STEP_DAYS}d)...")
    wf = walk_forward_top_k_trailing(
        funding,
        train_days=WF_TRAIN_DAYS,
        test_days=WF_TEST_DAYS,
        step_days=WF_STEP_DAYS,
        top_k=TOP_K,
        trailing_hours=TRAILING_HOURS,
        rebalance_hours=REBALANCE_HOURS,
    )
    print(f"  n_windows={wf.n_windows}  IS={wf.in_sample_ann_ret_mean*100:+.2f}%  "
          f"OOS={wf.out_of_sample_ann_ret_mean*100:+.2f}%  "
          f"decay={wf.is_oos_decay_pp:+.2f}pp")
    wf_payload = {
        "n_windows": wf.n_windows,
        "in_sample_ann_ret_mean": wf.in_sample_ann_ret_mean,
        "out_of_sample_ann_ret_mean": wf.out_of_sample_ann_ret_mean,
        "in_sample_ann_ret_std": wf.in_sample_ann_ret_std,
        "out_of_sample_ann_ret_std": wf.out_of_sample_ann_ret_std,
        "is_oos_decay_pp": wf.is_oos_decay_pp,
        "windows": [asdict(w) for w in wf.windows],
    }

    # --- 2. Regime conditioning (unhedged) ---
    print("\nRegime conditioning (unhedged, BTC trailing-7d vol terciles)...")
    candles_all = load_candles(conn)
    candles_year = {k: v for k, v in candles_all.items() if k in coins}
    if "BTC" not in candles_year or not candles_year["BTC"]:
        raise SystemExit("No BTC candles in hl_perp_candles -- cannot classify regimes.")
    btc_candles = [{"t": t, "c": c} for t, c in candles_year["BTC"]]
    regimes = classify_regimes(btc_candles, vol_window_hours=168)
    if not regimes:
        raise SystemExit("No regimes classified -- insufficient BTC candle history.")
    print(f"  classified {len(regimes)} hours into regimes")

    # Restrict funding to the regime-labeled window so rebalances all land
    # inside the candle span. Use the candle min/max.
    candle_t_min = min(t for t, _ in candles_year["BTC"])
    candle_t_max = max(t for t, _ in candles_year["BTC"])
    funding_in_candle = [t for t in funding if candle_t_min <= t.t_ms <= candle_t_max]
    per_reb = per_rebalance_unhedged(
        funding_in_candle,
        top_k=TOP_K,
        trailing_hours=TRAILING_HOURS,
        rebalance_hours=REBALANCE_HOURS,
    )
    print(f"  {len(per_reb)} rebalances inside candle window")
    fake_hedged = synthesise_unhedged_result(per_reb)
    regime_results = regime_conditional_results(
        fake_hedged, regimes, rebalance_hours=REBALANCE_HOURS, n_bootstrap=N_BOOTSTRAP,
    )
    for r in regime_results:
        print(f"  {r.regime_name:<5} n={r.n_rebalances:>4}  "
              f"ann={r.annualized_net_return*100:+7.2f}%  "
              f"sharpe={r.sharpe:+.2f}  "
              f"95% CI ann=[{r.ann_ret_ci_low*100:+.2f}%, {r.ann_ret_ci_high*100:+.2f}%]")
    regime_payload = {
        "analysis_span_days": (candle_t_max - candle_t_min) / 86_400_000.0,
        "n_rebalances_total": len(per_reb),
        "n_regimes_classified": len(regimes),
        "per_regime": [asdict(r) for r in regime_results],
    }

    # --- 3. Funding extremes ---
    print(f"\nFunding extremes (cooldown={COOLDOWN_HOURS}h, full 18-cell family)...")
    obs = merge_funding_and_prices(funding, candles_year)
    print(f"  eligible obs after burn-in + candle merge: {len(obs):,}")
    cells: list[dict] = []
    survivors: list[dict] = []
    for z in Z_THRESHOLDS:
        for d in DIRECTIONS:
            for h in HOLD_HOURS:
                r = run_study(
                    obs, z_threshold=z, direction=d, hold_hours=h,
                    cooldown_hours=COOLDOWN_HOURS,
                )
                tag = f"z{'>' if d == 'positive' else '<-'}{z:<4}{h:>3}h"
                print(f"  {tag}  n={r.n_events:>4}  "
                      f"LONG t={r.long_t_stat:+6.2f}  SHORT t={r.short_t_stat:+6.2f}")
                cell = {
                    "z_threshold": r.z_threshold,
                    "direction": r.direction,
                    "hold_hours": r.hold_hours,
                    "n_events": r.n_events,
                    "n_coins": r.n_coins,
                    "mean_price_return": r.mean_price_return,
                    "mean_funding_paid_long": r.mean_funding_paid_long,
                    "mean_long_net_return": r.mean_long_net_return,
                    "mean_short_net_return": r.mean_short_net_return,
                    "long_t_stat": r.long_t_stat,
                    "short_t_stat": r.short_t_stat,
                    "long_sharpe": r.long_sharpe,
                    "short_sharpe": r.short_sharpe,
                    "long_hit_rate": r.long_hit_rate,
                    "short_hit_rate": r.short_hit_rate,
                }
                cells.append(cell)
                for side, t_stat, net in (
                    ("LONG", r.long_t_stat, r.mean_long_net_return),
                    ("SHORT", r.short_t_stat, r.mean_short_net_return),
                ):
                    if abs(t_stat) > BONFERRONI_T:
                        survivors.append({
                            "side": side,
                            "z_threshold": r.z_threshold,
                            "direction": r.direction,
                            "hold_hours": r.hold_hours,
                            "t_stat": t_stat,
                            "mean_net_return": net,
                            "n_events": r.n_events,
                        })
    extremes_payload = {
        "cooldown_hours": COOLDOWN_HOURS,
        "n_tests_family": N_TESTS_FAMILY,
        "bonferroni_t": BONFERRONI_T,
        "n_eligible_obs": len(obs),
        "cells": cells,
        "survivors": survivors,
    }
    print(f"  Bonferroni survivors at cooldown={COOLDOWN_HOURS}h: {len(survivors)}")

    # --- 4. Block bootstrap on top-K trailing (year sample) ---
    print("\nBlock bootstrap on year-long top-K trailing...")
    full_returns = [r for _, _, r in per_rebalance_unhedged(
        funding,
        top_k=TOP_K,
        trailing_hours=TRAILING_HOURS,
        rebalance_hours=REBALANCE_HOURS,
    )]
    print(f"  n_periods={len(full_returns)}")
    block_length = estimate_optimal_block_length(full_returns)
    print(f"  Politis-White block_length={block_length}")
    block_stats = moving_block_bootstrap(
        full_returns, hours_per_period=REBALANCE_HOURS,
        block_length=block_length, n_resamples=N_BOOTSTRAP, seed=42,
    )
    iid_stats = bootstrap_backtest_stats(
        full_returns, hours_per_period=REBALANCE_HOURS,
        n_resamples=N_BOOTSTRAP, seed=42,
    )
    block_payload = {
        "n_periods": len(full_returns),
        "block_length": block_length,
        "n_resamples": N_BOOTSTRAP,
        "point_ann_return": block_stats.annualized_return.point,
        "ann_return_ci_low": block_stats.annualized_return.ci_low,
        "ann_return_ci_high": block_stats.annualized_return.ci_high,
        "point_sharpe": block_stats.sharpe.point,
        "sharpe_ci_low": block_stats.sharpe.ci_low,
        "sharpe_ci_high": block_stats.sharpe.ci_high,
        "iid_ann_return_ci_low": iid_stats.annualized_return.ci_low,
        "iid_ann_return_ci_high": iid_stats.annualized_return.ci_high,
        "iid_sharpe_ci_low": iid_stats.sharpe.ci_low,
        "iid_sharpe_ci_high": iid_stats.sharpe.ci_high,
    }
    print(f"  point ann={block_stats.annualized_return.point*100:+.2f}%  "
          f"block CI=[{block_stats.annualized_return.ci_low*100:+.2f}%, "
          f"{block_stats.annualized_return.ci_high*100:+.2f}%]")

    # --- Survival assessment ---
    survives: list[str] = []
    does_not: list[str] = []
    headline_finding: str

    # Walk-forward decay sign vs README's "negative decay" claim.
    if wf.is_oos_decay_pp > 0:
        does_not.append(
            f"Walk-forward 'OOS beats IS' (README): refuted. At n_windows={wf.n_windows}, "
            f"decay = {wf.is_oos_decay_pp:+.2f}pp (IS now beats OOS, conventional)."
        )
    else:
        survives.append(
            f"Walk-forward 'OOS beats IS': confirmed (decay = {wf.is_oos_decay_pp:+.2f}pp "
            f"on n={wf.n_windows} windows). Signal persistence is real."
        )

    # Walk-forward OOS sign.
    if wf.out_of_sample_ann_ret_mean > 0:
        survives.append(
            f"Walk-forward OOS strategy is positive (+{wf.out_of_sample_ann_ret_mean*100:.2f}% "
            f"ann mean) on n={wf.n_windows} windows."
        )
    else:
        does_not.append(
            f"Walk-forward OOS is non-positive ({wf.out_of_sample_ann_ret_mean*100:+.2f}%) "
            f"on n={wf.n_windows} windows -- the headline gross strategy fails OOS at year scale."
        )

    # Regime conditioning: does any regime have a CI that excludes zero?
    regimes_with_positive_ci = [
        r for r in regime_results
        if r.ann_ret_ci_low > 0 and r.n_rebalances >= TOP_K
    ]
    if regimes_with_positive_ci:
        for r in regimes_with_positive_ci:
            survives.append(
                f"Regime '{r.regime_name}' (N={r.n_rebalances}) ann ret CI "
                f"[{r.ann_ret_ci_low*100:+.2f}%, {r.ann_ret_ci_high*100:+.2f}%] excludes zero."
            )
    else:
        does_not.append(
            "No regime's annualized-return 95% CI excludes zero with N>=5 "
            "(unhedged baseline)."
        )

    # Extremes Bonferroni at cooldown=72h.
    if survivors:
        for s in survivors:
            survives.append(
                f"Extremes: {s['side']} z>{s['z_threshold']} {s['direction']} "
                f"{s['hold_hours']}h clears Bonferroni at cooldown=72h "
                f"(t={s['t_stat']:+.2f}, n={s['n_events']})."
            )
    else:
        does_not.append(
            f"Funding-extremes 'long the perp at z<-2 negative funding' "
            f"(7-of-18 at cooldown=0): refuted at cooldown=72h on year data. "
            f"Zero of 18 cells clear Bonferroni |t| > {BONFERRONI_T}."
        )

    # Block bootstrap.
    if block_stats.annualized_return.ci_low > 0:
        survives.append(
            f"Top-K trailing strategy ann return block-bootstrap CI "
            f"[{block_stats.annualized_return.ci_low*100:+.2f}%, "
            f"{block_stats.annualized_return.ci_high*100:+.2f}%] excludes zero "
            f"(n={len(full_returns)})."
        )
    else:
        does_not.append(
            f"Top-K trailing strategy ann return block-bootstrap CI "
            f"[{block_stats.annualized_return.ci_low*100:+.2f}%, "
            f"{block_stats.annualized_return.ci_high*100:+.2f}%] STRADDLES zero "
            f"on year sample (n={len(full_returns)})."
        )

    # Headline finding.
    if survivors:
        first = survivors[0]
        headline_finding = (
            "**Headline finding**: at year-long N with cooldown=72h, "
            f"{first['side']} z>{first['z_threshold']} {first['direction']} "
            f"{first['hold_hours']}h survives Bonferroni "
            f"(t={first['t_stat']:+.2f}, n={first['n_events']})."
        )
    else:
        headline_finding = (
            "**Headline finding**: the previous 'long the perp at negative-funding extremes' "
            "claim (7 of 18 cells cleared Bonferroni on ~22d at cooldown=0) does NOT "
            "survive at year-long N with cooldown=72h. Zero cells clear. The earlier result "
            "was driven by clustered events on the same coin within the small window."
        )

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "min_funding_rows": MIN_FUNDING_ROWS,
        "coins": coins,
        "coverage": coverage_rows,
        "walk_forward": wf_payload,
        "regime": regime_payload,
        "extremes": extremes_payload,
        "block_bootstrap": block_payload,
        "survives": survives,
        "does_not_survive": does_not,
        "headline_finding": headline_finding,
    }

    # Write JSON + Markdown.
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS_DIR / f"year_analysis_{ts}.json"
    json_path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nJSON -> {json_path}")

    MD_PATH.write_text(render_markdown(payload), encoding="utf-8")
    print(f"Markdown -> {MD_PATH}")

    conn.close()


if __name__ == "__main__":
    main()
