"""CLOB order-book depth analysis for Polymarket negRisk events.

The detector flags events on top-of-book gaps. A 150bp top-of-book gap is
only a real edge if you can sell into the book at meaningful size. This
module fetches the full /book for every YES token in a flagged event,
walks the book, and computes the per-market average fill price at a series
of notional levels — then aggregates into the event-level basket-trade
result.

Sell-side arb (sum of best_bid YES > 1):
  - For each market, walk the bid side from highest price down,
    consuming size until the cumulative notional reaches the target.
  - Average fill = sum(price * size) / sum(size) over consumed levels.
  - Basket cost-of-trade = sum across markets of avg_fill * (target_size).
  - Per-share PnL at settlement = avg_fill_sum - 1.0 (one market pays $1).

Buy-side arb (sum of best_ask YES < 1):
  - Same idea but walk the ask side from lowest price up.
  - Per-share PnL = 1.0 - avg_ask_fill_sum.

Important caveats called out in the module docstring of `report.py`:
  - Walking the visible book ignores hidden liquidity and the price impact
    of our own order.
  - The basket size is bottlenecked by the THINNEST market's depth — a
    150bp event-level gap can collapse to a sub-fee gap at any size above
    the thinnest market's near-top-of-book depth.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx

CLOB_BASE = "https://clob.polymarket.com"
DEFAULT_TIMEOUT = 30.0
RATE_LIMIT_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class Level:
    price: float
    size: float


@dataclass(frozen=True, slots=True)
class MarketBook:
    token_id: str
    bids: list[Level]   # sorted DESCENDING by price (best bid first)
    asks: list[Level]   # sorted ASCENDING by price (best ask first)


@dataclass(frozen=True, slots=True)
class WalkResult:
    """Result of walking one side of the book up to a target notional."""
    target_notional_usd: float
    consumed_notional_usd: float        # may be less than target if book exhausted
    consumed_shares: float
    avg_fill_price: float               # 0.0 if nothing consumed
    levels_walked: int
    book_exhausted: bool


@dataclass(frozen=True, slots=True)
class EventDepthResult:
    """Event-level basket trade summary at a given notional per market."""
    notional_per_market_usd: float
    n_markets: int
    direction: str                       # "sell_yes" | "buy_yes"
    sum_top_of_book: float               # naive sum (top of book)
    sum_avg_fill: float                  # depth-aware sum
    gap_top_of_book: float               # |sum_top - 1| as before
    gap_depth_aware: float               # |sum_avg_fill - 1| after walking
    basket_throttle_market: str          # the market that ran out of depth first
    basket_throttle_notional: float      # consumed notional in that market
    realized_pnl_per_share: float        # bid_gap-equivalent after depth adjustment


def _parse_book(payload: dict[str, Any]) -> MarketBook:
    bids = [Level(float(b["price"]), float(b["size"])) for b in payload.get("bids", [])]
    asks = [Level(float(a["price"]), float(a["size"])) for a in payload.get("asks", [])]
    bids.sort(key=lambda x: x.price, reverse=True)
    asks.sort(key=lambda x: x.price)
    return MarketBook(token_id=str(payload.get("asset_id")), bids=bids, asks=asks)


def walk_side(levels: Iterable[Level], target_notional_usd: float) -> WalkResult:
    """Consume size at each price level until the cumulative notional in USD
    meets `target_notional_usd`. Each share is worth `price` USD when sold at
    that level (or costs `price` USD when bought)."""
    spent_usd = 0.0
    shares = 0.0
    walked = 0
    for lvl in levels:
        if spent_usd >= target_notional_usd:
            break
        walked += 1
        if lvl.price <= 0:
            continue
        remaining_usd = target_notional_usd - spent_usd
        max_shares_here = remaining_usd / lvl.price
        take = min(lvl.size, max_shares_here)
        spent_usd += take * lvl.price
        shares += take
    exhausted = spent_usd < target_notional_usd - 1e-9
    avg = spent_usd / shares if shares > 0 else 0.0
    return WalkResult(
        target_notional_usd=target_notional_usd,
        consumed_notional_usd=spent_usd,
        consumed_shares=shares,
        avg_fill_price=avg,
        levels_walked=walked,
        book_exhausted=exhausted,
    )


async def fetch_books_for_event(
    markets: list[dict[str, Any]],
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, MarketBook]:
    """Fetch /book for every YES token in the given markets. Returns
    dict keyed by clobTokenIds[0] (the YES token)."""
    out: dict[str, MarketBook] = {}
    import json
    async with httpx.AsyncClient(timeout=timeout) as client:
        for m in markets:
            raw = m.get("clobTokenIds")
            if not raw:
                continue
            try:
                tokens = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                continue
            if not tokens:
                continue
            yes_id = str(tokens[0])
            r = await client.get(f"{CLOB_BASE}/book", params={"token_id": yes_id})
            if r.status_code != 200:
                continue
            payload = r.json()
            if not isinstance(payload, dict) or "bids" not in payload:
                continue
            out[yes_id] = _parse_book(payload)
            await asyncio.sleep(RATE_LIMIT_SECONDS)
    return out


def basket_sell_yes_depth(
    markets: list[dict[str, Any]],
    books: dict[str, MarketBook],
    *,
    notional_per_market_usd: float,
) -> EventDepthResult:
    """Sell-side basket: for each active market, walk the bid side selling
    `notional_per_market_usd` USD-worth of YES shares. Aggregate into a
    sum-of-avg-fill price across the event. Compare to sum of top-of-book."""
    import json
    sum_top = 0.0
    sum_avg = 0.0
    throttle_market = ""
    throttle_notional = float("inf")
    n = 0
    for m in markets:
        raw = m.get("clobTokenIds")
        if not raw:
            continue
        try:
            tokens = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        if not tokens:
            continue
        yes_id = str(tokens[0])
        book = books.get(yes_id)
        if book is None:
            continue
        bb = m.get("bestBid")
        if bb is None:
            continue
        sum_top += float(bb)
        walk = walk_side(book.bids, notional_per_market_usd)
        sum_avg += walk.avg_fill_price
        if walk.consumed_notional_usd < throttle_notional:
            throttle_notional = walk.consumed_notional_usd
            throttle_market = (m.get("question") or yes_id)[:50]
        n += 1
    return EventDepthResult(
        notional_per_market_usd=notional_per_market_usd,
        n_markets=n,
        direction="sell_yes",
        sum_top_of_book=sum_top,
        sum_avg_fill=sum_avg,
        gap_top_of_book=sum_top - 1.0,
        gap_depth_aware=sum_avg - 1.0,
        basket_throttle_market=throttle_market,
        basket_throttle_notional=throttle_notional,
        realized_pnl_per_share=sum_avg - 1.0,
    )


def basket_buy_yes_depth(
    markets: list[dict[str, Any]],
    books: dict[str, MarketBook],
    *,
    notional_per_market_usd: float,
) -> EventDepthResult:
    """Buy-side basket: for each active market, walk the ask side spending
    `notional_per_market_usd` USD on YES shares. Aggregate into a sum-of-
    avg-fill price across the event."""
    import json
    sum_top = 0.0
    sum_avg = 0.0
    throttle_market = ""
    throttle_notional = float("inf")
    n = 0
    for m in markets:
        raw = m.get("clobTokenIds")
        if not raw:
            continue
        try:
            tokens = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        if not tokens:
            continue
        yes_id = str(tokens[0])
        book = books.get(yes_id)
        if book is None:
            continue
        ba = m.get("bestAsk")
        if ba is None:
            continue
        sum_top += float(ba)
        walk = walk_side(book.asks, notional_per_market_usd)
        sum_avg += walk.avg_fill_price
        if walk.consumed_notional_usd < throttle_notional:
            throttle_notional = walk.consumed_notional_usd
            throttle_market = (m.get("question") or yes_id)[:50]
        n += 1
    return EventDepthResult(
        notional_per_market_usd=notional_per_market_usd,
        n_markets=n,
        direction="buy_yes",
        sum_top_of_book=sum_top,
        sum_avg_fill=sum_avg,
        gap_top_of_book=1.0 - sum_top,
        gap_depth_aware=1.0 - sum_avg,
        basket_throttle_market=throttle_market,
        basket_throttle_notional=throttle_notional,
        realized_pnl_per_share=1.0 - sum_avg,
    )
