"""Volume- and liquidity-weighted re-analysis of microstructure trap-rate classifications.

The existing scan reports "63% of detector flags are traps" by treating every
flagged event equally. That headline obscures the distribution of dollars-at-risk:
if two `real` events carry the bulk of the flagged volume, the dollar-weighted
trap rate is much lower than the count-based rate. This script joins the latest
microstructure classification scan with the events table to compute both
count-based and dollar-weighted breakdowns side by side, overall and by category.

Volume here is `events.volume` (lifetime cumulative USD) and liquidity is
`events.liquidity` (current resting size). Lifetime volume is a coarse proxy
for "dollars-at-risk now" -liquidity is reported alongside so the reader can
compare. Events with NULL volume/liquidity are treated as 0 with a stderr warning.

Usage:
    PYTHONPATH=src python scripts/volume_weighted_trap_rate.py
    PYTHONPATH=src python scripts/volume_weighted_trap_rate.py --db polymarket_edge.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

VERDICTS = ("real", "marginal", "trap")


@dataclass(frozen=True, slots=True)
class Row:
    event_id: str
    event_slug: str
    event_title: str
    category_tag: str
    verdict: str
    volume_usd: float
    liquidity_usd: float


def _latest_scan_id(conn: sqlite3.Connection) -> str | None:
    cur = conn.execute(
        "SELECT scan_id FROM microstructure_classifications "
        "ORDER BY classified_at DESC LIMIT 1"
    )
    row = cur.fetchone()
    return row[0] if row else None


def _load_rows(conn: sqlite3.Connection, scan_id: str) -> list[Row]:
    cur = conn.execute(
        """
        SELECT mc.event_id, mc.event_slug, mc.event_title, mc.category_tag,
               mc.verdict, e.volume, e.liquidity
        FROM microstructure_classifications mc
        LEFT JOIN events e ON e.id = mc.event_id
        WHERE mc.scan_id = ?
        ORDER BY mc.verdict, mc.event_slug
        """,
        (scan_id,),
    )
    rows: list[Row] = []
    null_volume_slugs: list[str] = []
    null_liquidity_slugs: list[str] = []
    for event_id, slug, title, cat, verdict, volume, liquidity in cur:
        v = float(volume) if volume is not None else 0.0
        liq = float(liquidity) if liquidity is not None else 0.0
        if volume is None:
            null_volume_slugs.append(slug or event_id)
        if liquidity is None:
            null_liquidity_slugs.append(slug or event_id)
        rows.append(Row(event_id, slug or "", title or "", cat, verdict, v, liq))
    if null_volume_slugs:
        print(
            f"WARNING: {len(null_volume_slugs)} event(s) had NULL volume "
            f"(treated as 0): {', '.join(null_volume_slugs)}",
            file=sys.stderr,
        )
    if null_liquidity_slugs:
        print(
            f"WARNING: {len(null_liquidity_slugs)} event(s) had NULL liquidity "
            f"(treated as 0): {', '.join(null_liquidity_slugs)}",
            file=sys.stderr,
        )
    return rows


def _share(num: float, denom: float) -> float:
    return (num / denom) if denom > 0 else 0.0


def _fmt_pct(x: float) -> str:
    pct = x * 100
    # Show extra precision for small non-zero values so they don't read as 0.0%.
    if 0 < pct < 0.1:
        return f"{pct:>6.3f}%"
    if 0 < pct < 1.0:
        return f"{pct:>6.2f}%"
    return f"{pct:>6.1f}%"


def _fmt_usd(x: float) -> str:
    if x >= 1_000_000_000:
        return f"${x / 1_000_000_000:.2f}B"
    if x >= 1_000_000:
        return f"${x / 1_000_000:.2f}M"
    if x >= 1_000:
        return f"${x / 1_000:.1f}K"
    return f"${x:.0f}"


def _overall_breakdown(rows: list[Row]) -> dict[str, dict[str, float]]:
    """{verdict: {count, count_share, volume, volume_share, liquidity, liquidity_share}}."""
    total_count = len(rows)
    total_volume = sum(r.volume_usd for r in rows)
    total_liquidity = sum(r.liquidity_usd for r in rows)
    out: dict[str, dict[str, float]] = {}
    for v in VERDICTS:
        subset = [r for r in rows if r.verdict == v]
        cnt = len(subset)
        vol = sum(r.volume_usd for r in subset)
        liq = sum(r.liquidity_usd for r in subset)
        out[v] = {
            "count": cnt,
            "count_share": _share(cnt, total_count),
            "volume": vol,
            "volume_share": _share(vol, total_volume),
            "liquidity": liq,
            "liquidity_share": _share(liq, total_liquidity),
        }
    out["_totals"] = {
        "count": total_count,
        "count_share": 1.0 if total_count else 0.0,
        "volume": total_volume,
        "volume_share": 1.0 if total_volume else 0.0,
        "liquidity": total_liquidity,
        "liquidity_share": 1.0 if total_liquidity else 0.0,
    }
    return out


def _category_breakdown(rows: list[Row]) -> list[dict[str, float | str | int]]:
    """One row per category_tag with count, volume, liquidity totals."""
    cats: dict[str, list[Row]] = {}
    for r in rows:
        cats.setdefault(r.category_tag, []).append(r)
    total_volume = sum(r.volume_usd for r in rows)
    out: list[dict[str, float | str | int]] = []
    for cat, subset in cats.items():
        cnt = len(subset)
        traps = sum(1 for r in subset if r.verdict == "trap")
        vol = sum(r.volume_usd for r in subset)
        liq = sum(r.liquidity_usd for r in subset)
        trap_vol = sum(r.volume_usd for r in subset if r.verdict == "trap")
        out.append(
            {
                "category": cat,
                "n": cnt,
                "n_trap": traps,
                "count_trap_rate": _share(traps, cnt),
                "volume": vol,
                "volume_share_of_total": _share(vol, total_volume),
                "trap_volume": trap_vol,
                "volume_trap_rate": _share(trap_vol, vol),
                "liquidity": liq,
            }
        )
    out.sort(key=lambda d: -float(d["volume_share_of_total"]))
    return out


def _print_overall_table(overall: dict[str, dict[str, float]]) -> None:
    print("Overall breakdown: count-based vs volume-weighted vs liquidity-weighted")
    print("-" * 100)
    header = (
        f"{'verdict':<10} {'count':>6} {'count_%':>8} "
        f"{'volume':>10} {'vol_%':>8} "
        f"{'liquidity':>11} {'liq_%':>8}"
    )
    print(header)
    print("-" * 100)
    for v in VERDICTS:
        r = overall[v]
        print(
            f"{v:<10} {int(r['count']):>6d} {_fmt_pct(r['count_share'])} "
            f"{_fmt_usd(r['volume']):>10} {_fmt_pct(r['volume_share'])} "
            f"{_fmt_usd(r['liquidity']):>11} {_fmt_pct(r['liquidity_share'])}"
        )
    t = overall["_totals"]
    print("-" * 100)
    print(
        f"{'TOTAL':<10} {int(t['count']):>6d} {_fmt_pct(1.0)} "
        f"{_fmt_usd(t['volume']):>10} {_fmt_pct(1.0)} "
        f"{_fmt_usd(t['liquidity']):>11} {_fmt_pct(1.0)}"
    )


def _print_category_table(cats: list[dict[str, float | str | int]]) -> None:
    print("\nPer-category breakdown (sorted by share of total flagged volume):")
    print("-" * 110)
    header = (
        f"{'category':<14} {'n':>3} {'n_trap':>6} {'count_trap%':>11} "
        f"{'volume':>10} {'vol_share':>9} {'trap_vol':>10} "
        f"{'vol_trap%':>9} {'liquidity':>11}"
    )
    print(header)
    print("-" * 110)
    for c in cats:
        print(
            f"{str(c['category'])[:14]:<14} {int(c['n']):>3d} "
            f"{int(c['n_trap']):>6d} {_fmt_pct(float(c['count_trap_rate']))} "
            f"{_fmt_usd(float(c['volume'])):>10} "
            f"{_fmt_pct(float(c['volume_share_of_total']))} "
            f"{_fmt_usd(float(c['trap_volume'])):>10} "
            f"{_fmt_pct(float(c['volume_trap_rate']))} "
            f"{_fmt_usd(float(c['liquidity'])):>11}"
        )


def _highest_volume(rows: list[Row], verdict: str) -> Row | None:
    subset = [r for r in rows if r.verdict == verdict]
    if not subset:
        return None
    return max(subset, key=lambda r: r.volume_usd)


def _print_named_events(rows: list[Row]) -> None:
    print("\nNamed events:")
    biggest_trap = _highest_volume(rows, "trap")
    biggest_real = _highest_volume(rows, "real")
    if biggest_trap is not None:
        print(
            f"  highest-volume trap:  {biggest_trap.event_slug} "
            f"({biggest_trap.category_tag}) - volume={_fmt_usd(biggest_trap.volume_usd)}, "
            f"liquidity={_fmt_usd(biggest_trap.liquidity_usd)}"
        )
    else:
        print("  highest-volume trap:  (none)")
    if biggest_real is not None:
        print(
            f"  highest-volume real:  {biggest_real.event_slug} "
            f"({biggest_real.category_tag}) - volume={_fmt_usd(biggest_real.volume_usd)}, "
            f"liquidity={_fmt_usd(biggest_real.liquidity_usd)}"
        )
    else:
        print("  highest-volume real:  (none)")


def _print_headline_rates(overall: dict[str, dict[str, float]]) -> None:
    print("\nTrap-rate headlines:")
    cnt_total = float(overall["_totals"]["count"])
    vol_total = float(overall["_totals"]["volume"])
    liq_total = float(overall["_totals"]["liquidity"])
    cnt_trap = float(overall["trap"]["count"])
    vol_trap = float(overall["trap"]["volume"])
    liq_trap = float(overall["trap"]["liquidity"])
    print(
        f"  count-based trap rate:        "
        f"{int(cnt_trap)}/{int(cnt_total)} = {_fmt_pct(_share(cnt_trap, cnt_total)).strip()}"
    )
    print(
        f"  volume-weighted trap rate:    "
        f"{_fmt_usd(vol_trap)}/{_fmt_usd(vol_total)} = "
        f"{_fmt_pct(_share(vol_trap, vol_total)).strip()}"
    )
    print(
        f"  liquidity-weighted trap rate: "
        f"{_fmt_usd(liq_trap)}/{_fmt_usd(liq_total)} = "
        f"{_fmt_pct(_share(liq_trap, liq_total)).strip()}"
    )


def _print_markdown(
    overall: dict[str, dict[str, float]],
    cats: list[dict[str, float | str | int]],
    rows: list[Row],
) -> None:
    print()
    print("=" * 100)
    print("Markdown (copy-paste into MICROSTRUCTURE.md):")
    print("=" * 100)
    print()
    print("**Overall: count vs volume vs liquidity**")
    print()
    print("| verdict | count | count share | volume (USD) | volume share | "
          "liquidity (USD) | liquidity share |")
    print("|---|---|---|---|---|---|---|")
    for v in VERDICTS:
        r = overall[v]
        bold = "**" if v == "trap" else ""
        print(
            f"| {bold}{v}{bold} | {int(r['count'])} | "
            f"{_fmt_pct(r['count_share']).strip()} | "
            f"{_fmt_usd(r['volume'])} | "
            f"{_fmt_pct(r['volume_share']).strip()} | "
            f"{_fmt_usd(r['liquidity'])} | "
            f"{_fmt_pct(r['liquidity_share']).strip()} |"
        )
    t = overall["_totals"]
    print(
        f"| total | {int(t['count'])} | 100.0% | "
        f"{_fmt_usd(t['volume'])} | 100.0% | "
        f"{_fmt_usd(t['liquidity'])} | 100.0% |"
    )

    print()
    print("**By category (sorted by share of total flagged volume)**")
    print()
    print(
        "| category | n | trap count | count trap rate | volume | volume share | "
        "trap volume | volume trap rate | liquidity |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    for c in cats:
        print(
            f"| {c['category']} | {int(c['n'])} | {int(c['n_trap'])} | "
            f"{_fmt_pct(float(c['count_trap_rate'])).strip()} | "
            f"{_fmt_usd(float(c['volume']))} | "
            f"{_fmt_pct(float(c['volume_share_of_total'])).strip()} | "
            f"{_fmt_usd(float(c['trap_volume']))} | "
            f"{_fmt_pct(float(c['volume_trap_rate'])).strip()} | "
            f"{_fmt_usd(float(c['liquidity']))} |"
        )

    biggest_trap = _highest_volume(rows, "trap")
    biggest_real = _highest_volume(rows, "real")
    print()
    print("**Named events**")
    print()
    if biggest_trap is not None:
        print(
            f"- highest-volume trap: `{biggest_trap.event_slug}` "
            f"({biggest_trap.category_tag}) - "
            f"volume {_fmt_usd(biggest_trap.volume_usd)}, "
            f"liquidity {_fmt_usd(biggest_trap.liquidity_usd)}"
        )
    if biggest_real is not None:
        print(
            f"- highest-volume real: `{biggest_real.event_slug}` "
            f"({biggest_real.category_tag}) - "
            f"volume {_fmt_usd(biggest_real.volume_usd)}, "
            f"liquidity {_fmt_usd(biggest_real.liquidity_usd)}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=Path("polymarket_edge.db"),
        help="SQLite DB path (default: ./polymarket_edge.db)",
    )
    parser.add_argument(
        "--scan-id", type=str, default=None,
        help="Specific scan_id to analyse (default: latest by classified_at)",
    )
    args = parser.parse_args()

    if not args.db.exists():
        print(f"ERROR: db not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    try:
        scan_id = args.scan_id or _latest_scan_id(conn)
        if scan_id is None:
            print(
                "ERROR: no rows in microstructure_classifications -"
                "run scripts/microstructure_scan.py first",
                file=sys.stderr,
            )
            sys.exit(2)
        rows = _load_rows(conn, scan_id)
        if not rows:
            print(f"ERROR: scan_id {scan_id!r} has no rows", file=sys.stderr)
            sys.exit(3)
    finally:
        conn.close()

    print(f"scan_id={scan_id} ({len(rows)} classified events)\n")
    overall = _overall_breakdown(rows)
    _print_overall_table(overall)
    cats = _category_breakdown(rows)
    _print_category_table(cats)
    _print_named_events(rows)
    _print_headline_rates(overall)
    _print_markdown(overall, cats, rows)


if __name__ == "__main__":
    main()
