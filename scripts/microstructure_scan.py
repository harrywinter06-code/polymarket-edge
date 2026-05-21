"""Depth-aware trap-rate scan across all currently-active Polymarket negRisk events.

Pulls every active event from gamma, scores each via the top-of-book detector,
walks /book at $50/market and $500/market for each flagged event, and classifies
the verdict ('real' / 'marginal' / 'trap'). Prints per-event and per-category
tables, then persists every classification to SQLite.

Usage:
    PYTHONPATH=src python scripts/microstructure_scan.py
    PYTHONPATH=src python scripts/microstructure_scan.py --max-events 200

Progress is logged to stderr; results to stdout (so the table can be redirected).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polymarket_edge import db, fetch  # noqa: E402
from polymarket_edge.microstructure import (  # noqa: E402
    EventClassification,
    aggregate_by_category,
    scan_and_classify,
)

VERDICT_ORDER = ["real", "marginal", "trap", "noise"]


def _print_per_event_table(classifications: list[EventClassification]) -> None:
    """Per-event table to stdout."""
    if not classifications:
        print("(no flagged events classified)")
        return
    header = (
        f"{'slug':<48} {'category':<22} {'n':>3} "
        f"{'top_bp':>8} {'gap50_bp':>10} {'gap500_bp':>10} "
        f"{'throttle_$':>11} {'verdict':<10}"
    )
    print(header)
    print("-" * len(header))
    # Sort: real first, then marginal, then trap; within each, by event slug
    order = {"real": 0, "marginal": 1, "trap": 2, "noise": 3}
    sorted_cls = sorted(
        classifications,
        key=lambda c: (order.get(c.verdict, 9), c.event_slug),
    )
    for c in sorted_cls:
        slug = (c.event_slug or c.event_id)[:48]
        cat = (c.category_tag or "")[:22]
        print(
            f"{slug:<48} {cat:<22} {c.n_markets:>3d} "
            f"{c.top_of_book_gap * 10000:>+8.1f} "
            f"{c.gap_at_small_size * 10000:>+10.1f} "
            f"{c.gap_at_med_size * 10000:>+10.1f} "
            f"{c.throttle_notional_usd:>11.2f} "
            f"{c.verdict:<10}"
        )


def _print_aggregate_table(aggregate: dict[str, dict[str, int]]) -> None:
    if not aggregate:
        print("\n(no per-category aggregate — nothing to summarise)")
        return
    print("\nPer-category aggregate (verdict counts):")
    header = (
        f"{'category':<28} {'real':>5} {'marginal':>9} {'trap':>5} "
        f"{'noise':>6} {'total':>6} {'trap_rate':>10}"
    )
    print(header)
    print("-" * len(header))
    rows = []
    for cat, verdicts in aggregate.items():
        real = verdicts.get("real", 0)
        marginal = verdicts.get("marginal", 0)
        trap = verdicts.get("trap", 0)
        noise = verdicts.get("noise", 0)
        total = real + marginal + trap + noise
        denom = real + marginal + trap  # exclude noise from trap-rate denom
        trap_rate = (trap / denom) if denom > 0 else 0.0
        rows.append((cat, real, marginal, trap, noise, total, trap_rate))
    # Sort by total flagged events descending
    rows.sort(key=lambda r: -r[5])
    for cat, real, marginal, trap, noise, total, trap_rate in rows:
        print(
            f"{cat[:28]:<28} {real:>5d} {marginal:>9d} {trap:>5d} "
            f"{noise:>6d} {total:>6d} {trap_rate * 100:>9.1f}%"
        )


def _print_headlines(classifications: list[EventClassification]) -> None:
    if not classifications:
        return
    n = len(classifications)
    n_real = sum(1 for c in classifications if c.verdict == "real")
    n_marg = sum(1 for c in classifications if c.verdict == "marginal")
    n_trap = sum(1 for c in classifications if c.verdict == "trap")
    n_noise = sum(1 for c in classifications if c.verdict == "noise")
    print("\nHeadline rates across all detector-flagged events:")
    print(f"  total classified:    {n}")
    print(f"  real        (signal holds at $500/mkt):  {n_real:>4d}  ({n_real / n * 100:.1f}%)")
    print(f"  marginal    (decays inside fee buffer):  {n_marg:>4d}  ({n_marg / n * 100:.1f}%)")
    print(
        f"  trap        (inverts to loss at $50/mkt): {n_trap:>4d}  ({n_trap / n * 100:.1f}%)"
    )
    if n_noise:
        pct = n_noise / n * 100
        print(f"  noise       (under fee buffer):           {n_noise:>4d}  ({pct:.1f}%)")
    print("\nTrap rate (across flagged-real/marginal/trap, excluding noise):")
    denom = n_real + n_marg + n_trap
    if denom > 0:
        print(f"  {n_trap}/{denom} = {n_trap / denom * 100:.1f}%")
    else:
        print("  (no non-noise classifications)")


def _persist(
    db_path: Path,
    classifications: list[EventClassification],
    scan_id: str,
) -> None:
    conn = db.connect(db_path)
    db.init_schema(conn)
    classified_at = fetch.now_iso()
    for c in classifications:
        conn.execute(
            """
            INSERT INTO microstructure_classifications
            (scan_id, event_id, event_slug, event_title, category_tag,
             n_markets, neg_risk_augmented, direction, top_of_book_gap,
             gap_at_small_size, gap_at_med_size, throttle_notional_usd,
             verdict, classified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_id,
                c.event_id,
                c.event_slug,
                c.event_title,
                c.category_tag,
                c.n_markets,
                1 if c.neg_risk_augmented else 0,
                c.direction,
                c.top_of_book_gap,
                c.gap_at_small_size,
                c.gap_at_med_size,
                c.throttle_notional_usd,
                c.verdict,
                classified_at,
            ),
        )
    conn.commit()
    print(f"\npersisted {len(classifications)} rows to {db_path} (scan_id={scan_id})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-events", type=int, default=500,
        help="Cap on active events fetched from gamma (default: 500)",
    )
    parser.add_argument(
        "--small-size-usd", type=float, default=50.0,
        help="Per-market notional for 'small' depth probe (default: $50)",
    )
    parser.add_argument(
        "--med-size-usd", type=float, default=500.0,
        help="Per-market notional for 'medium' depth probe (default: $500)",
    )
    parser.add_argument(
        "--fee-buffer", type=float, default=0.0050,
        help="Min top-of-book gap to flag (default: 0.0050 = 50bp)",
    )
    parser.add_argument(
        "--db", type=Path, default=Path("polymarket_edge.db"),
        help="SQLite DB path (default: ./polymarket_edge.db)",
    )
    args = parser.parse_args()

    classifications = asyncio.run(
        scan_and_classify(
            max_events=args.max_events,
            small_size_usd=args.small_size_usd,
            med_size_usd=args.med_size_usd,
            fee_buffer=args.fee_buffer,
        )
    )

    print("=" * 100)
    print("Per-event classifications")
    print("=" * 100)
    _print_per_event_table(classifications)

    aggregate = aggregate_by_category(classifications)
    _print_aggregate_table(aggregate)
    _print_headlines(classifications)

    scan_id = uuid.uuid4().hex[:12]
    _persist(args.db, classifications, scan_id)


if __name__ == "__main__":
    main()
