"""Depth-aware trap-rate classification across all active Polymarket negRisk events.

The detector flags candidate arbs on top-of-book gaps. Three case studies in
the README showed the top-of-book signal can be (a) real, (b) marginal, or
(c) an outright trap once depth is walked. This module generalises that
case-by-case observation into a population statistic: for every currently-
flagged event, walk the book at small ($50/market) and medium ($500/market)
notionals and assign a verdict.

Verdict logic (binary, evaluated on the flagged direction only):

  - 'real':     top_of_book_gap >  +fee_buffer  AND  gap_at_med_size > +fee_buffer
                (signal holds through institutional-scale notional)
  - 'trap':     top_of_book_gap >  +fee_buffer  AND  gap_at_small_size < 0
                (signal inverts to a loss at retail-scale notional;
                 implies at least one market has near-zero depth)
  - 'marginal': top_of_book_gap >  +fee_buffer  AND  gap_at_small_size >= 0
                                                AND  gap_at_med_size <= +fee_buffer
                (signal holds at retail size but decays out of the fee buffer
                 before institutional size)
  - 'noise':    top_of_book_gap <= +fee_buffer
                (not flagged by the detector — included only if explicitly kept)

The 'flagged direction' is whichever of sell_yes / buy_yes shows the larger
positive gap at top of book; if both are flagged, the larger is picked.
Sell-side flags walk the bid side of each market; buy-side flags walk the
ask side. The two directions are NEVER mixed within a single classification
(no half-and-half basket trades).
"""

from __future__ import annotations

import sqlite3
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from polymarket_edge import book_depth, detector, fetch


@dataclass(frozen=True, slots=True)
class EventClassification:
    event_id: str
    event_slug: str
    event_title: str
    category_tag: str            # from event['tags'][0]['label'] or 'Uncategorized'
    n_markets: int
    neg_risk_augmented: bool
    top_of_book_gap: float       # signed; positive = flagged direction
    direction: str               # 'sell_yes' | 'buy_yes'
    gap_at_small_size: float     # depth-aware gap at small_size_usd/market
    gap_at_med_size: float       # depth-aware gap at med_size_usd/market
    throttle_notional_usd: float # min consumed across markets at med_size_usd target
    verdict: str                 # 'real' | 'marginal' | 'trap' | 'noise'


def _extract_category(event: dict[str, Any]) -> str:
    """First tag's label, or 'Uncategorized' if no tags."""
    tags = event.get("tags")
    if not tags or not isinstance(tags, list):
        return "Uncategorized"
    first = tags[0]
    if not isinstance(first, dict):
        return "Uncategorized"
    label = first.get("label")
    if not label or not isinstance(label, str):
        return "Uncategorized"
    return label


def _active_markets(event: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        m for m in event.get("markets", [])
        if m.get("active") and not m.get("closed") and m.get("acceptingOrders")
    ]


def classify_event(
    event: dict[str, Any],
    books: dict[str, book_depth.MarketBook],
    *,
    small_size_usd: float = 50.0,
    med_size_usd: float = 500.0,
    fee_buffer: float = 0.0050,
) -> EventClassification | None:
    """Classify a single event by walking the flagged side at two notionals.

    Returns None if the event is not negRisk, has < 2 active markets, or any
    active market has a missing top-of-book quote (detector's existing rule).
    """
    sig = detector.score_event(event)
    if sig is None:
        return None

    markets = _active_markets(event)
    category = _extract_category(event)
    title = event.get("title") or ""
    slug = event.get("slug") or ""
    augmented = bool(event.get("negRiskAugmented"))

    # Pick the flagged direction: whichever gap is larger at top-of-book.
    # Both can be positive (rare); pick the larger.
    if sig.bid_gap >= sig.ask_gap:
        direction = "sell_yes"
        top_gap = sig.bid_gap
        walk_fn = book_depth.basket_sell_yes_depth
    else:
        direction = "buy_yes"
        top_gap = sig.ask_gap
        walk_fn = book_depth.basket_buy_yes_depth

    small_result = walk_fn(markets, books, notional_per_market_usd=small_size_usd)
    med_result = walk_fn(markets, books, notional_per_market_usd=med_size_usd)

    # If we couldn't walk enough markets (e.g. /book returned 404 for half the
    # legs), the gap_depth_aware would be misleading — treat as un-classifiable.
    if small_result.n_markets < sig.n_markets or med_result.n_markets < sig.n_markets:
        return None

    gap_small = small_result.gap_depth_aware
    gap_med = med_result.gap_depth_aware
    throttle = med_result.basket_throttle_notional

    if top_gap <= fee_buffer:
        verdict = "noise"
    elif gap_small < 0:
        verdict = "trap"
    elif gap_med > fee_buffer:
        verdict = "real"
    else:
        verdict = "marginal"

    return EventClassification(
        event_id=sig.event_id,
        event_slug=slug,
        event_title=title,
        category_tag=category,
        n_markets=sig.n_markets,
        neg_risk_augmented=augmented,
        top_of_book_gap=top_gap,
        direction=direction,
        gap_at_small_size=gap_small,
        gap_at_med_size=gap_med,
        throttle_notional_usd=throttle,
        verdict=verdict,
    )


async def scan_and_classify(
    *,
    max_events: int | None = 500,
    small_size_usd: float = 50.0,
    med_size_usd: float = 500.0,
    fee_buffer: float = 0.0050,
    include_noise: bool = False,
) -> list[EventClassification]:
    """Pull active events, score each, walk books for events flagged by the detector.

    Returns a list of EventClassification (one per classifiable flagged event).
    Skips:
      - non-negRisk events
      - negRisk events with < 2 active markets or missing quotes
      - events whose top-of-book gap does not exceed fee_buffer (unless
        include_noise=True)
      - events where /book returned errors for any active market

    Progress is logged to stderr.
    """
    print(f"fetching active events (cap={max_events})...", file=sys.stderr)
    events = await fetch.fetch_all_active_events(max_events=max_events)
    print(f"fetched {len(events)} active events", file=sys.stderr)

    # Phase 1: score every event with the existing top-of-book detector.
    flagged_events: list[dict[str, Any]] = []
    for ev in events:
        sig = detector.score_event(ev)
        if sig is None:
            continue
        if sig.best_gap <= fee_buffer and not include_noise:
            continue
        flagged_events.append(ev)
    print(
        f"detector flagged {len(flagged_events)} events at fee_buffer={fee_buffer:.4f}",
        file=sys.stderr,
    )

    # Phase 2: for each flagged event, fetch /book per market and classify.
    classifications: list[EventClassification] = []
    skipped_book_error = 0
    for i, ev in enumerate(flagged_events, 1):
        slug = ev.get("slug", "?")
        markets = _active_markets(ev)
        print(
            f"  [{i}/{len(flagged_events)}] {slug} ({len(markets)} markets)...",
            file=sys.stderr,
        )
        try:
            books = await book_depth.fetch_books_for_event(markets)
        except Exception as e:  # network boundary: log and continue across all errors
            print(f"    book fetch failed: {e!r}", file=sys.stderr)
            skipped_book_error += 1
            continue

        cls = classify_event(
            ev,
            books,
            small_size_usd=small_size_usd,
            med_size_usd=med_size_usd,
            fee_buffer=fee_buffer,
        )
        if cls is None:
            skipped_book_error += 1
            print("    skipped (partial/missing books)", file=sys.stderr)
            continue
        print(
            f"    verdict={cls.verdict}  top={cls.top_of_book_gap * 10000:+.0f}bp  "
            f"med={cls.gap_at_med_size * 10000:+.0f}bp  cat={cls.category_tag}",
            file=sys.stderr,
        )
        classifications.append(cls)

    print(
        f"classified {len(classifications)} events "
        f"({skipped_book_error} skipped on book errors)",
        file=sys.stderr,
    )
    return classifications


def aggregate_by_category(
    classifications: list[EventClassification],
) -> dict[str, dict[str, int]]:
    """Return {category: {'real': n, 'marginal': n, 'trap': n, 'noise': n}}.

    Categories with zero count for a verdict still show 0; verdicts not present
    in any classification are omitted from the inner dict's keys."""
    out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for c in classifications:
        out[c.category_tag][c.verdict] += 1
    # Normalise to plain dicts so callers don't get defaultdict surprises.
    return {cat: dict(verdicts) for cat, verdicts in out.items()}


def persist_classifications(
    conn: sqlite3.Connection,
    classifications: list[EventClassification],
    *,
    scan_id: str | None = None,
) -> str:
    """Insert each classification into `microstructure_classifications`.

    Returns the scan_id used (auto-generated if not provided). Caller is
    responsible for `db.init_schema(conn)` if the schema may not exist.
    """
    scan_id = scan_id or uuid.uuid4().hex[:12]
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
    return scan_id
