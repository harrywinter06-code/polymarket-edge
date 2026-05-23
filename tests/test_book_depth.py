"""Tests for the depth-walking math and the /book HTTP fetcher."""

from __future__ import annotations

import asyncio
import json

import httpx

from polymarket_edge import book_depth
from polymarket_edge.book_depth import (
    Level,
    MarketBook,
    basket_buy_yes_depth,
    basket_sell_yes_depth,
    fetch_books_for_event,
    walk_side,
)


def test_walk_empty_book() -> None:
    r = walk_side([], target_notional_usd=100.0)
    assert r.consumed_notional_usd == 0.0
    assert r.consumed_shares == 0.0
    assert r.avg_fill_price == 0.0
    assert r.book_exhausted is True


def test_walk_fills_at_single_deep_level() -> None:
    # One level at 0.50 with 1000 shares = $500 of depth; selling $100 stays at 0.50.
    levels = [Level(0.50, 1000.0)]
    r = walk_side(levels, target_notional_usd=100.0)
    assert abs(r.consumed_notional_usd - 100.0) < 1e-9
    assert abs(r.consumed_shares - 200.0) < 1e-9   # 100 / 0.50
    assert abs(r.avg_fill_price - 0.50) < 1e-9
    assert r.book_exhausted is False


def test_walk_walks_through_multiple_levels() -> None:
    # Level 1: 0.50 x 100 shares = $50 of depth
    # Level 2: 0.40 x 100 shares = $40 of depth
    # Selling $80: consume all of level 1 ($50, 100 shares), then $30 of level 2
    # ($30 / 0.40 = 75 shares). Total: 175 shares, $80. Avg = 80 / 175 = 0.4571.
    levels = [Level(0.50, 100.0), Level(0.40, 100.0)]
    r = walk_side(levels, target_notional_usd=80.0)
    assert abs(r.consumed_notional_usd - 80.0) < 1e-9
    assert abs(r.consumed_shares - 175.0) < 1e-9
    assert abs(r.avg_fill_price - (80.0 / 175.0)) < 1e-9
    assert r.book_exhausted is False
    assert r.levels_walked == 2


def test_walk_marks_exhausted_when_book_runs_out() -> None:
    # Two levels totaling $90 of depth; asking for $200.
    levels = [Level(0.50, 100.0), Level(0.40, 100.0)]
    r = walk_side(levels, target_notional_usd=200.0)
    assert abs(r.consumed_notional_usd - 90.0) < 1e-9
    assert abs(r.consumed_shares - 200.0) < 1e-9   # 100 + 100
    assert abs(r.avg_fill_price - (90.0 / 200.0)) < 1e-9
    assert r.book_exhausted is True


def test_walk_skips_zero_price_levels() -> None:
    # Level at price=0 contributes no value; second level fills the order.
    levels = [Level(0.0, 1_000_000.0), Level(0.50, 1000.0)]
    r = walk_side(levels, target_notional_usd=10.0)
    assert abs(r.consumed_notional_usd - 10.0) < 1e-9
    assert abs(r.avg_fill_price - 0.50) < 1e-9


def test_basket_throttle_bottleneck() -> None:
    """The basket trade is throttled by the thinnest market's depth.

    Synthetic: two markets, market A has $100 of depth at 0.50, market B
    has $5 of depth at 0.50. At $50/market target, A fills full, B exhausts.
    The 'throttle' is market B with $5 actually consumed."""
    from polymarket_edge.book_depth import (
        Level,
        MarketBook,
        basket_sell_yes_depth,
        walk_side,
    )

    markets = [
        {"clobTokenIds": '["1"]', "bestBid": 0.50, "question": "market A",
         "active": True, "closed": False, "acceptingOrders": True},
        {"clobTokenIds": '["2"]', "bestBid": 0.50, "question": "market B",
         "active": True, "closed": False, "acceptingOrders": True},
    ]
    books = {
        "1": MarketBook(token_id="1", bids=[Level(0.50, 200.0)], asks=[]),
        "2": MarketBook(token_id="2", bids=[Level(0.50, 10.0)], asks=[]),
    }
    # Sanity on walk:
    assert abs(walk_side(books["1"].bids, 50.0).consumed_notional_usd - 50.0) < 1e-9
    assert abs(walk_side(books["2"].bids, 50.0).consumed_notional_usd - 5.0) < 1e-9
    r = basket_sell_yes_depth(markets, books, notional_per_market_usd=50.0)
    assert r.n_markets == 2
    assert abs(r.basket_throttle_notional - 5.0) < 1e-9
    assert "market B" in r.basket_throttle_market


def test_basket_buy_yes_depth_walks_asks() -> None:
    markets = [
        {"clobTokenIds": '["1"]', "bestAsk": 0.55, "question": "A",
         "active": True, "closed": False, "acceptingOrders": True},
    ]
    books = {"1": MarketBook(token_id="1", bids=[], asks=[Level(0.55, 1000.0)])}
    r = basket_buy_yes_depth(markets, books, notional_per_market_usd=50.0)
    assert r.direction == "buy_yes"
    assert r.n_markets == 1
    assert r.sum_top_of_book == 0.55
    assert abs(r.sum_avg_fill - 0.55) < 1e-9


def test_basket_skips_markets_with_missing_books() -> None:
    """A market whose token isn't in the books dict is silently skipped — the
    market count drops but the result is still well-formed."""
    markets = [
        {"clobTokenIds": '["1"]', "bestBid": 0.50, "question": "A",
         "active": True, "closed": False, "acceptingOrders": True},
        {"clobTokenIds": '["2"]', "bestBid": 0.40, "question": "B",
         "active": True, "closed": False, "acceptingOrders": True},
    ]
    books = {"1": MarketBook(token_id="1", bids=[Level(0.50, 200.0)], asks=[])}
    r = basket_sell_yes_depth(markets, books, notional_per_market_usd=50.0)
    assert r.n_markets == 1  # market B was skipped


def test_basket_skips_markets_with_missing_quotes() -> None:
    markets = [
        {"clobTokenIds": '["1"]', "bestBid": None, "question": "A",
         "active": True, "closed": False, "acceptingOrders": True},
    ]
    books = {"1": MarketBook(token_id="1", bids=[Level(0.50, 100.0)], asks=[])}
    r = basket_sell_yes_depth(markets, books, notional_per_market_usd=50.0)
    assert r.n_markets == 0


def test_basket_handles_malformed_clob_token_ids() -> None:
    """clobTokenIds that is not valid JSON, or empty, should skip the market
    rather than raise."""
    markets = [
        {"clobTokenIds": "not-json", "bestBid": 0.50,
         "active": True, "closed": False, "acceptingOrders": True},
        {"clobTokenIds": "[]", "bestBid": 0.50,
         "active": True, "closed": False, "acceptingOrders": True},
        {"bestBid": 0.50,  # missing clobTokenIds entirely
         "active": True, "closed": False, "acceptingOrders": True},
    ]
    r = basket_sell_yes_depth(markets, {}, notional_per_market_usd=50.0)
    assert r.n_markets == 0


def test_fetch_books_for_event_parses_books(mock_http) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        token_id = request.url.params.get("token_id")
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "asset_id": token_id,
                    "bids": [{"price": "0.50", "size": "100"}, {"price": "0.49", "size": "50"}],
                    "asks": [{"price": "0.55", "size": "200"}],
                }
            ).encode(),
        )

    mock_http(handler)
    markets = [
        {"clobTokenIds": '["yes-1", "no-1"]'},
        {"clobTokenIds": ["yes-2", "no-2"]},  # already-decoded list
    ]
    books = asyncio.run(fetch_books_for_event(markets))
    assert set(books.keys()) == {"yes-1", "yes-2"}
    # Bids sorted descending, asks ascending.
    yb = books["yes-1"]
    assert yb.bids[0].price == 0.50 and yb.bids[1].price == 0.49
    assert yb.asks[0].price == 0.55


def test_fetch_books_for_event_skips_non_200_and_malformed(mock_http) -> None:
    """A 404 or malformed payload should be skipped for that token (no raise)."""

    def handler(request: httpx.Request) -> httpx.Response:
        token_id = request.url.params.get("token_id")
        if token_id == "yes-1":
            return httpx.Response(404, content=b"{}")
        if token_id == "yes-2":
            return httpx.Response(200, content=b'"not-a-dict"')
        return httpx.Response(
            200,
            content=json.dumps(
                {"asset_id": token_id, "bids": [{"price": "0.5", "size": "1"}], "asks": []}
            ).encode(),
        )

    mock_http(handler)
    markets = [
        {"clobTokenIds": '["yes-1", "no-1"]'},
        {"clobTokenIds": '["yes-2", "no-2"]'},
        {"clobTokenIds": '["yes-3", "no-3"]'},
        {"clobTokenIds": ""},          # empty -> skip
        {"clobTokenIds": "not-json"},  # malformed -> skip
    ]
    books = asyncio.run(fetch_books_for_event(markets))
    assert set(books.keys()) == {"yes-3"}


def test_fetch_books_for_event_returns_empty_when_no_markets() -> None:
    """Edge: zero markets in — zero books out, no HTTP needed."""
    out = asyncio.run(fetch_books_for_event([]))
    assert out == {}


def test_book_depth_module_constants() -> None:
    """Pin the public constants so accidental edits to defaults get caught."""
    assert book_depth.CLOB_BASE.startswith("https://")
    assert book_depth.DEFAULT_TIMEOUT > 0
