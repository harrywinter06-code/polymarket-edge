"""Tests for the depth-walking math."""

from __future__ import annotations

from polymarket_edge.book_depth import Level, walk_side


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
