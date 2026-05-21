"""Tests for the depth-aware trap-rate classifier."""

from __future__ import annotations

from typing import Any

from polymarket_edge.book_depth import Level, MarketBook
from polymarket_edge.microstructure import (
    EventClassification,
    aggregate_by_category,
    classify_event,
)


def _market(
    bid: float,
    ask: float,
    token_id: str,
    *,
    question: str = "q",
    **overrides: Any,
) -> dict[str, Any]:
    m: dict[str, Any] = {
        "id": token_id,
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "bestBid": bid,
        "bestAsk": ask,
        "question": question,
        "clobTokenIds": f'["{token_id}"]',
    }
    m.update(overrides)
    return m


def _event(
    markets: list[dict[str, Any]],
    *,
    neg_risk: bool = True,
    tag: str | None = "Sports",
    augmented: bool = False,
) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "id": "ev1",
        "slug": "test-event",
        "title": "Test Event",
        "negRisk": neg_risk,
        "negRiskAugmented": augmented,
        "markets": markets,
    }
    if tag is not None:
        ev["tags"] = [{"label": tag}]
    return ev


def test_classify_event_returns_none_for_non_neg_risk() -> None:
    ev = _event(
        [
            _market(0.55, 0.56, "1"),
            _market(0.55, 0.56, "2"),
        ],
        neg_risk=False,
    )
    assert classify_event(ev, books={}) is None


def test_classify_event_returns_none_for_single_market() -> None:
    ev = _event([_market(0.60, 0.61, "1")])
    assert classify_event(ev, books={}) is None


def test_classify_event_flags_real_signal() -> None:
    """Two markets, sum_bid = 1.10. Both have $1000+ deep books at exactly the
    top-of-book price — depth-aware gap stays at top-of-book gap at $500/market.
    Top-of-book gap > fee buffer, gap@$500 > fee buffer => 'real'."""
    markets = [
        _market(0.55, 0.56, "1", question="A"),
        _market(0.55, 0.56, "2", question="B"),
    ]
    books = {
        "1": MarketBook(token_id="1", bids=[Level(0.55, 5000.0)], asks=[]),
        "2": MarketBook(token_id="2", bids=[Level(0.55, 5000.0)], asks=[]),
    }
    ev = _event(markets, tag="Sports")
    cls = classify_event(ev, books, fee_buffer=0.0050)
    assert isinstance(cls, EventClassification)
    assert cls.verdict == "real"
    assert cls.direction == "sell_yes"
    assert abs(cls.top_of_book_gap - 0.10) < 1e-9
    assert abs(cls.gap_at_med_size - 0.10) < 1e-9
    assert cls.category_tag == "Sports"


def test_classify_event_flags_trap() -> None:
    """Two markets, sum_bid = 1.10 at top-of-book. Market A is deep, market B
    has only $3 of bid depth — walking $50/market in B exhausts the book to
    near-zero, basket avg fill collapses, gap@$50 inverts negative => 'trap'."""
    markets = [
        _market(0.55, 0.56, "1", question="A-deep"),
        _market(0.55, 0.56, "2", question="B-thin"),
    ]
    books = {
        "1": MarketBook(token_id="1", bids=[Level(0.55, 10_000.0)], asks=[]),
        # Thin: only $3 of bid depth, then nothing. Walking $50 exhausts the
        # book; avg_fill = 3/(3/0.55) = 0.55 still, but consumed_notional = $3,
        # so we need a deeper price-collapse below to drive negative. Use a
        # tiny top level and a much-lower second level.
        "2": MarketBook(
            token_id="2",
            bids=[Level(0.55, 5.0), Level(0.01, 100_000.0)],  # $2.75 then near-zero
            asks=[],
        ),
    }
    ev = _event(markets, tag="Culture")
    cls = classify_event(ev, books, fee_buffer=0.0050)
    assert isinstance(cls, EventClassification)
    # At $50/market in market B, we eat $2.75 at 0.55 then $47.25 at 0.01.
    # avg_fill_B = 50 / (5 + 4725) ≈ 0.0106. sum_avg = 0.55 + 0.0106 = 0.5606.
    # gap_small = 0.5606 - 1 = -0.439 (very negative). => trap.
    assert cls.verdict == "trap"
    assert cls.gap_at_small_size < 0


def test_classify_event_flags_marginal() -> None:
    """Top-of-book gap > fee buffer; depth holds positive at $50/market but
    decays into the fee buffer by $500/market. Verdict 'marginal'."""
    # Setup: two markets at 0.55 bid. Each book has $50 of size at 0.55, then
    # the rest at 0.51 (still positive but lower).
    #   At $50/market: walk fully at 0.55 -> avg=0.55 -> sum=1.10 -> gap=0.10 > 0.5bp
    #   At $500/market: $50 at 0.55 ($27.5 spent, 50 shares), remaining $472.5
    #     at 0.51 -> 472.5/0.51 = 926.47 shares. Total: 976.47 shares, $500.
    #     avg = 500/976.47 = 0.5121. sum_avg = 1.0241. gap_med = 0.0241.
    # We want gap_med <= fee_buffer. Pick fee_buffer = 0.0250.
    markets = [
        _market(0.55, 0.56, "1", question="A"),
        _market(0.55, 0.56, "2", question="B"),
    ]
    books = {
        "1": MarketBook(
            token_id="1",
            bids=[Level(0.55, 50.0), Level(0.51, 10_000.0)],
            asks=[],
        ),
        "2": MarketBook(
            token_id="2",
            bids=[Level(0.55, 50.0), Level(0.51, 10_000.0)],
            asks=[],
        ),
    }
    ev = _event(markets, tag="Sports")
    cls = classify_event(ev, books, fee_buffer=0.0250)
    assert isinstance(cls, EventClassification)
    assert cls.gap_at_small_size > 0
    assert cls.gap_at_med_size <= 0.0250
    assert cls.verdict == "marginal"


def test_classify_event_handles_missing_tags() -> None:
    markets = [
        _market(0.55, 0.56, "1"),
        _market(0.55, 0.56, "2"),
    ]
    books = {
        "1": MarketBook(token_id="1", bids=[Level(0.55, 5000.0)], asks=[]),
        "2": MarketBook(token_id="2", bids=[Level(0.55, 5000.0)], asks=[]),
    }
    ev = _event(markets, tag=None)
    cls = classify_event(ev, books)
    assert cls is not None
    assert cls.category_tag == "Uncategorized"


def test_classify_event_returns_none_for_partial_books() -> None:
    """If /book returned for some markets but not all, classification is
    untrustworthy — we return None and let the caller skip the event."""
    markets = [
        _market(0.55, 0.56, "1"),
        _market(0.55, 0.56, "2"),
    ]
    # Only market 1's book is present.
    books = {
        "1": MarketBook(token_id="1", bids=[Level(0.55, 5000.0)], asks=[]),
    }
    ev = _event(markets)
    assert classify_event(ev, books) is None


def test_classify_event_picks_buy_side_when_ask_gap_larger() -> None:
    """If both bid and ask gaps are positive (rare), pick the larger."""
    # sum_bid = 1.02 (bid_gap = +0.02), sum_ask = 0.90 (ask_gap = +0.10).
    # Larger is ask_gap => buy_yes direction.
    markets = [
        _market(0.51, 0.45, "1"),
        _market(0.51, 0.45, "2"),
    ]
    books = {
        "1": MarketBook(
            token_id="1", bids=[], asks=[Level(0.45, 5000.0)],
        ),
        "2": MarketBook(
            token_id="2", bids=[], asks=[Level(0.45, 5000.0)],
        ),
    }
    ev = _event(markets)
    cls = classify_event(ev, books, fee_buffer=0.0050)
    assert cls is not None
    assert cls.direction == "buy_yes"
    assert abs(cls.top_of_book_gap - 0.10) < 1e-9


def test_aggregate_by_category_counts_correctly() -> None:
    cls_list = [
        EventClassification(
            event_id="1", event_slug="a", event_title="A", category_tag="Sports",
            n_markets=2, neg_risk_augmented=False, top_of_book_gap=0.10,
            direction="sell_yes", gap_at_small_size=0.10, gap_at_med_size=0.10,
            throttle_notional_usd=1000.0, verdict="real",
        ),
        EventClassification(
            event_id="2", event_slug="b", event_title="B", category_tag="Sports",
            n_markets=4, neg_risk_augmented=False, top_of_book_gap=0.06,
            direction="sell_yes", gap_at_small_size=-0.20, gap_at_med_size=-0.50,
            throttle_notional_usd=5.0, verdict="trap",
        ),
        EventClassification(
            event_id="3", event_slug="c", event_title="C", category_tag="Politics",
            n_markets=2, neg_risk_augmented=True, top_of_book_gap=0.08,
            direction="buy_yes", gap_at_small_size=0.02, gap_at_med_size=0.001,
            throttle_notional_usd=500.0, verdict="marginal",
        ),
    ]
    agg = aggregate_by_category(cls_list)
    assert agg == {
        "Sports": {"real": 1, "trap": 1},
        "Politics": {"marginal": 1},
    }
