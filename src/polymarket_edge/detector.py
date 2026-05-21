"""Event-level no-arb detector for Polymarket negRisk events.

For a negRisk event with N mutually-exclusive markets, the YES-side probabilities
across all markets should sum to 1.0 in a fair market. Deviations imply arb:

  - sum(best_bid_yes) > 1: sell-side arb (sell YES across all, settle one at $1)
  - sum(best_ask_yes) < 1: buy-side arb (buy YES across all, one settles at $1)

Both gaps must exceed a fee buffer to be a real opportunity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class EventArbSignal:
    event_id: str
    title: str | None
    slug: str | None
    n_markets: int
    sum_best_bid: float
    sum_best_ask: float
    bid_gap: float  # sum_best_bid - 1.0  (positive => sell-side arb candidate)
    ask_gap: float  # 1.0 - sum_best_ask  (positive => buy-side arb candidate)
    has_neg_risk_other: bool

    @property
    def direction(self) -> str:
        if self.bid_gap > 0 and self.ask_gap > 0:
            return "both"
        if self.bid_gap > 0:
            return "sell_yes"
        if self.ask_gap > 0:
            return "buy_yes"
        return "none"

    @property
    def best_gap(self) -> float:
        return max(self.bid_gap, self.ask_gap)


def score_event(event: dict[str, Any]) -> EventArbSignal | None:
    """Return a signal for a negRisk event, or None if not applicable.

    Skips events that are not negRisk, have fewer than 2 active markets, or
    have any missing quote on an active market (the sum bound is undefined
    if even one leg is unquoted).
    """
    if not event.get("negRisk"):
        return None

    active_markets: list[dict[str, Any]] = [
        m for m in event.get("markets", [])
        if m.get("active")
        and not m.get("closed")
        and m.get("acceptingOrders")
    ]
    if len(active_markets) < 2:
        return None

    bids: list[float] = []
    asks: list[float] = []
    has_other = False
    for m in active_markets:
        if m.get("negRiskOther"):
            has_other = True
        bb = m.get("bestBid")
        ba = m.get("bestAsk")
        if bb is None or ba is None:
            return None
        bids.append(float(bb))
        asks.append(float(ba))

    sum_bid = sum(bids)
    sum_ask = sum(asks)
    return EventArbSignal(
        event_id=str(event["id"]),
        title=event.get("title"),
        slug=event.get("slug"),
        n_markets=len(active_markets),
        sum_best_bid=sum_bid,
        sum_best_ask=sum_ask,
        bid_gap=sum_bid - 1.0,
        ask_gap=1.0 - sum_ask,
        has_neg_risk_other=has_other,
    )


def is_flagged(signal: EventArbSignal, *, fee_buffer: float = 0.02) -> bool:
    """True iff either gap exceeds the fee buffer (covers ~2% taker fee)."""
    return signal.bid_gap > fee_buffer or signal.ask_gap > fee_buffer
