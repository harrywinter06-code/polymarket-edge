"""Plan D — Hyperliquid funding-extreme directional study.

Loads funding from `hl_funding_history`, fetches (or reads cached) hourly perp
candles into `hl_perp_candles`, joins on hour-buckets with a strictly trailing
168h z-score, then runs the 18-test family:

    z_threshold in {1.5, 2.0, 2.5}  x  direction in {positive, negative}
    x  hold_hours in {6, 24, 72}

Reports each cell, the family-wide Bonferroni survivors (|t| > 3.05), and a
per-coin breakdown on the liquid universe (BTC, ETH, SOL, XRP, DOGE).

Usage:
    PYTHONPATH=src python scripts/hl_extremes_study.py
    PYTHONPATH=src python scripts/hl_extremes_study.py --no-fetch        # use DB cache
    PYTHONPATH=src python scripts/hl_extremes_study.py --days 22

Output JSON: results/hl_extremes_<timestamp>.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polymarket_edge import db  # noqa: E402
from polymarket_edge.hl_backtest import load_funding  # noqa: E402
from polymarket_edge.hl_extremes import (  # noqa: E402
    TRAILING_HOURS,
    ExtremeStudyResult,
    fetch_perp_candles_for_universe,
    load_candles,
    merge_funding_and_prices,
    run_study,
)

# --- experiment grid -------------------------------------------------------

Z_THRESHOLDS = (1.5, 2.0, 2.5)
DIRECTIONS = ("positive", "negative")
HOLD_HOURS = (6, 24, 72)
N_TESTS = len(Z_THRESHOLDS) * len(DIRECTIONS) * len(HOLD_HOURS)  # 18
# Bonferroni-corrected single-sided two-tailed: alpha=0.05 / 18 -> z ~= 3.05
BONFERRONI_T = 3.05
LIQUID_UNIVERSE = ("BTC", "ETH", "SOL", "XRP", "DOGE")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=str(ROOT / "polymarket_edge.db"))
    p.add_argument("--days", type=int, default=22)
    p.add_argument(
        "--no-fetch",
        action="store_true",
        help="Skip the network candle pull; use hl_perp_candles from the DB.",
    )
    p.add_argument(
        "--results-dir",
        default=str(ROOT / "results"),
        help="Where to write the JSON output (created if missing).",
    )
    return p.parse_args()


def _print_eligible_count(funding_rows: list, candles: dict) -> int:
    obs = merge_funding_and_prices(funding_rows, candles)
    print(
        f"universe: {len({t.coin for t in funding_rows})} coins, "
        f"{len(funding_rows):,} funding observations"
    )
    print(
        f"eligible after dropping first {TRAILING_HOURS}h per coin and "
        f"merging on candle hours: {len(obs):,}"
    )
    return len(obs)


def _fmt_pct(x: float) -> str:
    return f"{x * 100:+.3f}%"


def _print_study_row(r: ExtremeStudyResult, *, label: str) -> None:
    print(
        f"  {label}  n={r.n_events:>4d}  coins={r.n_coins:>2d}  "
        f"price={_fmt_pct(r.mean_price_return)}  fund={_fmt_pct(r.mean_funding_paid_long)}  "
        f"LONG_net={_fmt_pct(r.mean_long_net_return)} sharpe={r.long_sharpe:+.2f} "
        f"t={r.long_t_stat:+.2f} hit={r.long_hit_rate * 100:.1f}%  "
        f"SHORT_net={_fmt_pct(r.mean_short_net_return)} t={r.short_t_stat:+.2f} "
        f"hit={r.short_hit_rate * 100:.1f}%"
    )


def _run_family(obs: list, *, label: str, cooldown_hours: int) -> list[ExtremeStudyResult]:
    print(f"\n=== {label} (cooldown={cooldown_hours}h) ===")
    results: list[ExtremeStudyResult] = []
    for z in Z_THRESHOLDS:
        for d in DIRECTIONS:
            for h in HOLD_HOURS:
                r = run_study(
                    obs,
                    z_threshold=z,
                    direction=d,
                    hold_hours=h,
                    cooldown_hours=cooldown_hours,
                )
                results.append(r)
                tag = f"z{'>' if d == 'positive' else '<-'}{z:<4}  {h:>2d}h"
                _print_study_row(r, label=tag)
    return results


def _print_bonferroni_survivors(results: list[ExtremeStudyResult]) -> list[dict]:
    print(
        f"\nBonferroni: 18-test family, |t| > {BONFERRONI_T:.2f} required "
        f"(single-test alpha=0.05/18)"
    )
    survivors: list[dict] = []
    for r in results:
        for side, t in (("LONG", r.long_t_stat), ("SHORT", r.short_t_stat)):
            if abs(t) > BONFERRONI_T:
                survivors.append(
                    {
                        "z_threshold": r.z_threshold,
                        "direction": r.direction,
                        "hold_hours": r.hold_hours,
                        "side": side,
                        "t_stat": t,
                        "mean_net_return": (
                            r.mean_long_net_return if side == "LONG"
                            else r.mean_short_net_return
                        ),
                        "n_events": r.n_events,
                    }
                )
    if not survivors:
        # Find the closest miss across all 36 (long+short) slots.
        all_t = [
            ("LONG", r, r.long_t_stat) for r in results
        ] + [("SHORT", r, r.short_t_stat) for r in results]
        all_t.sort(key=lambda x: abs(x[2]), reverse=True)
        side, r, t = all_t[0]
        print(
            f"  None clear Bonferroni. Closest: {side} z={r.z_threshold} "
            f"{r.direction} {r.hold_hours}h  t={t:+.2f}  n={r.n_events}"
        )
    else:
        for s in survivors:
            print(
                f"  CLEARS:  {s['side']}  z>{s['z_threshold']} {s['direction']} "
                f"{s['hold_hours']}h  t={s['t_stat']:+.2f}  "
                f"net={_fmt_pct(s['mean_net_return'])}  n={s['n_events']}"
            )
    return survivors


def _print_liquid_breakdown(obs: list, *, z: float, hold: int) -> dict[str, dict]:
    print(
        f"\nPer-coin breakdown (liquid universe, z>{z} positive, "
        f"{hold}h hold, cooldown=0):"
    )
    print(f"  {'coin':<6} {'n_events':>9} {'long_net':>10} {'sharpe':>8} {'t':>6}")
    by_coin_obs = [o for o in obs if o.coin in LIQUID_UNIVERSE]
    out: dict[str, dict] = {}
    for coin in LIQUID_UNIVERSE:
        coin_obs = [o for o in by_coin_obs if o.coin == coin]
        r = run_study(
            coin_obs, z_threshold=z, direction="positive", hold_hours=hold,
        )
        out[coin] = {
            "n_events": r.n_events,
            "mean_long_net_return": r.mean_long_net_return,
            "long_sharpe": r.long_sharpe,
            "long_t_stat": r.long_t_stat,
        }
        print(
            f"  {coin:<6} {r.n_events:>9d} {_fmt_pct(r.mean_long_net_return):>10} "
            f"{r.long_sharpe:>+8.2f} {r.long_t_stat:>+6.2f}"
        )
    # Also report the aggregated study over the liquid-universe subset.
    r_all = run_study(
        by_coin_obs, z_threshold=z, direction="positive", hold_hours=hold,
    )
    print(
        f"  {'LIQUID':<6} {r_all.n_events:>9d} "
        f"{_fmt_pct(r_all.mean_long_net_return):>10} "
        f"{r_all.long_sharpe:>+8.2f} {r_all.long_t_stat:>+6.2f}"
    )
    out["LIQUID_AGGREGATE"] = {
        "n_events": r_all.n_events,
        "mean_long_net_return": r_all.mean_long_net_return,
        "long_sharpe": r_all.long_sharpe,
        "long_t_stat": r_all.long_t_stat,
        "mean_short_net_return": r_all.mean_short_net_return,
        "short_t_stat": r_all.short_t_stat,
    }
    return out


def _result_to_dict(r: ExtremeStudyResult) -> dict:
    d = asdict(r)
    # Strip the long event list to keep the JSON manageable; keep summary stats.
    d["events"] = [
        {
            "coin": e.coin,
            "entry_t_ms": e.entry_t_ms,
            "entry_z": e.entry_z,
            "long_net_return": e.long_net_return,
            "short_net_return": e.short_net_return,
        }
        for e in r.events
    ]
    return d


async def main() -> None:
    args = _parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    conn = db.connect(db_path)
    db.init_schema(conn)

    # 1. Load funding from DB
    funding_rows = load_funding(conn)
    if not funding_rows:
        raise SystemExit("hl_funding_history is empty — run the funding ingest first.")
    coins = sorted({t.coin for t in funding_rows})

    # 2. Fetch perp candles (or read from cache)
    if args.no_fetch:
        candles = load_candles(conn)
        print(
            f"loaded {sum(len(v) for v in candles.values()):,} cached candle rows "
            f"across {len(candles)} coins from hl_perp_candles"
        )
    else:
        print(f"fetching {args.days}d hourly candles for {len(coins)} coins...")
        t0 = time.time()
        candles = await fetch_perp_candles_for_universe(
            coins, days=args.days, db_path=str(db_path)
        )
        dt = time.time() - t0
        n_rows = sum(len(v) for v in candles.values())
        print(f"  -> {n_rows:,} candle rows in {dt:.1f}s, persisted to hl_perp_candles")

    n_eligible = _print_eligible_count(funding_rows, candles)
    if n_eligible == 0:
        raise SystemExit("No eligible (funding, price) observations — abort.")

    obs = merge_funding_and_prices(funding_rows, candles)

    # 3. Run the full family on the whole universe, no cooldown
    full_results_no_cd = _run_family(obs, label="FULL UNIVERSE", cooldown_hours=0)

    # 4. Independence check — re-run with cooldown = hold_hours (per-grid, so we
    # re-run the family with cooldown=72h matching the longest hold). This is
    # the conservative independence run; if the conclusions differ between
    # cooldown=0 and cooldown=72, the cooldown=72 numbers are more credible.
    full_results_cd = _run_family(obs, label="FULL UNIVERSE", cooldown_hours=72)

    # 5. Liquid-only subset
    obs_liquid = [o for o in obs if o.coin in LIQUID_UNIVERSE]
    print(
        f"\nliquid universe: {LIQUID_UNIVERSE}, "
        f"{len(obs_liquid):,} eligible observations"
    )
    liquid_results_no_cd = _run_family(obs_liquid, label="LIQUID UNIVERSE", cooldown_hours=0)

    # 6. Bonferroni — apply to the headline (full-universe, cooldown=0) family
    survivors = _print_bonferroni_survivors(full_results_no_cd)
    print("\n(also checking independence run: full-universe, cooldown=72)")
    survivors_cd = _print_bonferroni_survivors(full_results_cd)
    print("\n(also checking liquid universe, cooldown=0)")
    survivors_liquid = _print_bonferroni_survivors(liquid_results_no_cd)

    # 7. Per-coin breakdown at the headline cell (z>2, 24h, positive)
    per_coin = _print_liquid_breakdown(obs, z=2.0, hold=24)

    # 8. Persist
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%S")
    out_path = results_dir / f"hl_extremes_{ts}.json"
    payload = {
        "generated_at": ts,
        "n_coins": len(coins),
        "n_funding_rows": len(funding_rows),
        "n_eligible_obs": n_eligible,
        "z_thresholds": list(Z_THRESHOLDS),
        "directions": list(DIRECTIONS),
        "hold_hours": list(HOLD_HOURS),
        "n_tests_family": N_TESTS,
        "bonferroni_t_threshold": BONFERRONI_T,
        "liquid_universe": list(LIQUID_UNIVERSE),
        "full_universe_cooldown_0": [_result_to_dict(r) for r in full_results_no_cd],
        "full_universe_cooldown_72": [_result_to_dict(r) for r in full_results_cd],
        "liquid_universe_cooldown_0": [_result_to_dict(r) for r in liquid_results_no_cd],
        "bonferroni_survivors_full_cd0": survivors,
        "bonferroni_survivors_full_cd72": survivors_cd,
        "bonferroni_survivors_liquid_cd0": survivors_liquid,
        "per_coin_z2_24h": per_coin,
    }
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"\nSaved -> {out_path}")
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
