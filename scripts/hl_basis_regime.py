"""Plan B — basis-hedged Hyperliquid funding-capture, regime-conditional.

Pulls funding from the local SQLite, hits Hyperliquid for ``spotMeta`` and
1h perp + spot candles over the same window, runs the hedged backtest under
two spread regimes (no extra spread; 5 bps/leg), and prints regime-conditional
stats with bootstrap CIs.

Run via: ``PYTHONPATH=src python scripts/hl_basis_regime.py``

The output answers two distinct questions:
  (1) What's the basis-hedged result of the existing strategy? (no extra spread)
  (2) Does adding execution cost still produce a positive regime? (5 bps/leg)
"""

from __future__ import annotations

import asyncio
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from polymarket_edge import db, hl_backtest
from polymarket_edge.hl_basis_hedge import (
    HOUR_MS,
    HedgedBacktestResult,
    backtest_hedged_top_k_trailing,
    classify_regimes,
    detect_spot_listings,
    fetch_perp_and_spot_candles,
    merge_to_hedged_ticks,
    regime_conditional_results,
)
from polymarket_edge.hl_stats import (
    bootstrap_backtest_stats,
    compute_per_period_returns_trailing,
)

DB_PATH = "polymarket_edge.db"
TOP_K = 5
TRAILING_HOURS = 24
REBALANCE_HOURS = 8
N_BOOTSTRAP = 2000


@dataclass(frozen=True)
class UnhedgedSummary:
    n_rebalances: int
    annualized_return: float
    sharpe: float
    ci_low: float
    ci_high: float


def _fmt_pct(x: float) -> str:
    return f"{x * 100:+8.2f}%"


def _fmt_sharpe(x: float) -> str:
    return f"{x:+7.2f}"


def _iso(t_ms: int) -> str:
    return datetime.fromtimestamp(t_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M")


def _compute_unhedged_baseline(
    ticks: Sequence[hl_backtest.FundingTick],
) -> UnhedgedSummary:
    per_period = compute_per_period_returns_trailing(
        ticks, top_k=TOP_K, trailing_hours=TRAILING_HOURS,
        rebalance_hours=REBALANCE_HOURS,
    )
    if not per_period:
        return UnhedgedSummary(0, 0.0, 0.0, 0.0, 0.0)
    stats = bootstrap_backtest_stats(
        per_period, hours_per_period=REBALANCE_HOURS,
        n_resamples=N_BOOTSTRAP, seed=42,
    )
    return UnhedgedSummary(
        n_rebalances=len(per_period),
        annualized_return=stats.annualized_return.point,
        sharpe=stats.sharpe.point,
        ci_low=stats.annualized_return.ci_low,
        ci_high=stats.annualized_return.ci_high,
    )


def _print_headline(label: str, r: HedgedBacktestResult) -> None:
    print(
        f"  {label:<48}  ann_ret={_fmt_pct(r.annualized_net_return)}  "
        f"sharpe={_fmt_sharpe(r.sharpe)}  n={r.n_rebalances}  "
        f"max_dd={r.max_drawdown * 100:6.2f}%  hit={r.hit_rate * 100:5.1f}%"
    )


def _print_regime_table(
    label: str, regime_results: list, *, top_k: int = 3,
) -> tuple[str | None, float, float]:
    """Print the per-regime table. Returns (best_regime_name, best_ann_ret, best_sharpe)
    where 'best' = highest annualized return among regimes with N>=top_k."""
    print(f"\n{label}")
    print(
        f"  {'regime':<7} {'n':>3}  {'ann_ret':>9}  {'sharpe':>7}  "
        f"{'sharpe_95CI':<22}  {'ann_95CI':<24}  {'max_dd':>7}"
    )
    best_name: str | None = None
    best_ret = float("-inf")
    best_sharpe = 0.0
    for rc in regime_results:
        ci_s = f"[{rc.sharpe_ci_low:+5.2f}, {rc.sharpe_ci_high:+5.2f}]"
        ci_r = f"[{rc.ann_ret_ci_low * 100:+6.2f}%, {rc.ann_ret_ci_high * 100:+6.2f}%]"
        print(
            f"  {rc.regime_name:<7} {rc.n_rebalances:>3}  "
            f"{_fmt_pct(rc.annualized_net_return)}  {_fmt_sharpe(rc.sharpe)}  "
            f"{ci_s:<22}  {ci_r:<24}  {rc.max_drawdown * 100:6.2f}%"
        )
        if rc.n_rebalances >= top_k and rc.annualized_net_return > best_ret:
            best_ret = rc.annualized_net_return
            best_sharpe = rc.sharpe
            best_name = rc.regime_name
    return best_name, best_ret, best_sharpe


async def _run() -> None:
    conn = db.connect(DB_PATH)
    ticks = hl_backtest.load_funding(conn)
    if not ticks:
        print("No funding data in DB. Run `polymarket-edge hl-history` first.")
        return
    universe_coins = sorted({t.coin for t in ticks})
    t_min = min(t.t_ms for t in ticks)
    t_max = max(t.t_ms for t in ticks)
    days_span = max(1, (t_max - t_min) // 86_400_000)

    print(f"universe: {len(universe_coins)} coins")
    print(f"data window: {_iso(t_min)} -> {_iso(t_max)} ({days_span} days)")

    print("\nProbing spotMeta for hedgeable subset...")
    have_spot, no_spot, label_map = await detect_spot_listings(universe_coins)
    print(f"  with spot ({len(have_spot)}):    {have_spot}")
    print(f"  perp-only ({len(no_spot)}):     {no_spot}")

    print(f"\nFetching 1h perp + spot candles over {days_span}d window...")
    candles = await fetch_perp_and_spot_candles(
        have_spot, days=days_span + 1, spot_label_map=label_map, end_ms=t_max + HOUR_MS,
    )
    n_perp_total = sum(len(v["perp"]) for v in candles.values())
    n_spot_total = sum(len(v["spot"]) for v in candles.values())
    print(f"  perp candles: {n_perp_total:,}   spot candles: {n_spot_total:,}")

    hedged_ticks = merge_to_hedged_ticks(ticks, candles)
    coins_after_merge = sorted({h.coin for h in hedged_ticks})
    print(
        f"  hedged-tick coins after merge: {len(coins_after_merge)} "
        f"({coins_after_merge})"
    )

    print("\n" + "=" * 78)
    print("UNHEDGED baseline (all universe coins, funding-only, no spread):")
    unhedged = _compute_unhedged_baseline(ticks)
    print(
        f"  ann_ret={_fmt_pct(unhedged.annualized_return)}  "
        f"sharpe={_fmt_sharpe(unhedged.sharpe)}  "
        f"n_rebalances={unhedged.n_rebalances}  "
        f"ann_95CI=[{unhedged.ci_low * 100:+6.2f}%, {unhedged.ci_high * 100:+6.2f}%]"
    )

    print("\nHEDGED (spot-eligible coins, basis modeled):")
    no_spread = backtest_hedged_top_k_trailing(
        hedged_ticks, top_k=TOP_K, trailing_hours=TRAILING_HOURS,
        rebalance_hours=REBALANCE_HOURS, entry_spread_bps_per_leg=None,
    )
    _print_headline("no extra spread", no_spread)
    print(
        f"    basis pnl contribution:    {_fmt_pct(no_spread.annualized_basis_pnl)} ann"
    )
    print(
        f"    funding-only contribution: "
        f"{_fmt_pct(no_spread.annualized_funding_only)} ann"
    )
    delta_vs_unhedged = no_spread.annualized_net_return - unhedged.annualized_return
    print(
        f"    delta vs unhedged-all-coins baseline: {_fmt_pct(delta_vs_unhedged)} ann "
        f"(coin-universe shrinkage + basis effect combined)"
    )

    with_spread = backtest_hedged_top_k_trailing(
        hedged_ticks, top_k=TOP_K, trailing_hours=TRAILING_HOURS,
        rebalance_hours=REBALANCE_HOURS, entry_spread_bps_per_leg=5.0,
    )
    _print_headline("5 bps/leg (20 bps round-trip)", with_spread)

    # Bootstrap CI on the headline hedged Sharpe (across all regimes).
    headline_returns = [rb.net_return for rb in no_spread.rebalances]
    if headline_returns:
        headline_stats = bootstrap_backtest_stats(
            headline_returns, hours_per_period=REBALANCE_HOURS,
            n_resamples=N_BOOTSTRAP, seed=42,
        )
        print(
            f"    hedged-headline 95% CI: "
            f"ann=[{headline_stats.annualized_return.ci_low * 100:+6.2f}%, "
            f"{headline_stats.annualized_return.ci_high * 100:+6.2f}%]  "
            f"sharpe=[{headline_stats.sharpe.ci_low:+5.2f}, "
            f"{headline_stats.sharpe.ci_high:+5.2f}]"
        )

    # Regime classification needs BTC perp candles spanning the analysis window
    # plus a 7-day warm-up. The candles we already fetched for BTC cover this if
    # BTC is in the spot-eligible set; otherwise pull BTC perp separately.
    btc_candles = candles.get("BTC", {}).get("perp") if "BTC" in candles else None
    if not btc_candles:
        # BTC must be in the universe — fall back to fetching just BTC perp.
        print("  (BTC not in spot-eligible set; fetching BTC perp for regime classifier)")
        extra = await fetch_perp_and_spot_candles(
            ["BTC"], days=days_span + 1, spot_label_map={}, end_ms=t_max + HOUR_MS,
        )
        btc_candles = extra["BTC"]["perp"]

    regimes = classify_regimes(btc_candles, vol_window_hours=168)
    print(
        f"\nBTC trailing-7d realized vol regime cutoffs computed "
        f"({len(regimes)} hours classified)"
    )
    if regimes:
        vols = sorted(r.btc_realized_vol_trailing_7d for r in regimes.values())
        med = statistics.median(vols)
        print(
            f"  hourly-vol range across window: "
            f"min={vols[0] * 100:6.4f}%  med={med * 100:6.4f}%  max={vols[-1] * 100:6.4f}%"
        )

    rc_no_spread = regime_conditional_results(
        no_spread, regimes, rebalance_hours=REBALANCE_HOURS, n_bootstrap=N_BOOTSTRAP,
    )
    rc_with_spread = regime_conditional_results(
        with_spread, regimes, rebalance_hours=REBALANCE_HOURS, n_bootstrap=N_BOOTSTRAP,
    )

    best_ns_name, best_ns_ret, best_ns_sharpe = _print_regime_table(
        "Regime-conditional (HEDGED + no extra spread):", rc_no_spread,
    )
    best_5_name, best_5_ret, best_5_sharpe = _print_regime_table(
        "Regime-conditional (HEDGED + 5 bps/leg):", rc_with_spread,
    )

    print("\n" + "=" * 78)
    if best_ns_name is not None:
        print(
            f"Best regime under HEDGED + no spread: '{best_ns_name}' "
            f"ann_ret={_fmt_pct(best_ns_ret)} sharpe={_fmt_sharpe(best_ns_sharpe)}"
        )
    if best_5_name is not None:
        print(
            f"Best surviving regime under 5 bps/leg: '{best_5_name}' "
            f"ann_ret={_fmt_pct(best_5_ret)} sharpe={_fmt_sharpe(best_5_sharpe)}"
        )
    else:
        print("No regime survives 5 bps/leg (all annualized returns negative).")
    print(
        "\nNote: N per regime is small (~N/3 of total rebalances). Sharpe CIs "
        "are wide; treat regime claims as directional."
    )


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
