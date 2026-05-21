"""Tests for the event-level no-arb detector."""

from __future__ import annotations

from typing import Any

from polymarket_edge.detector import EventArbSignal, is_flagged, score_event


def _market(bid: float | None, ask: float | None, **overrides: Any) -> dict[str, Any]:
    m: dict[str, Any] = {
        "id": "1",
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "bestBid": bid,
        "bestAsk": ask,
    }
    m.update(overrides)
    return m


def test_skips_non_neg_risk_event() -> None:
    event = {
        "id": "e1",
        "negRisk": False,
        "markets": [_market(0.4, 0.5), _market(0.4, 0.5)],
    }
    assert score_event(event) is None


def test_skips_event_with_single_active_market() -> None:
    event = {"id": "e2", "negRisk": True, "markets": [_market(0.4, 0.5)]}
    assert score_event(event) is None


def test_skips_when_quote_missing() -> None:
    event = {
        "id": "e3",
        "negRisk": True,
        "markets": [
            _market(0.4, 0.5),
            _market(None, 0.5),
        ],
    }
    assert score_event(event) is None


def test_skips_closed_or_non_accepting_markets() -> None:
    event = {
        "id": "e3b",
        "negRisk": True,
        "markets": [
            _market(0.4, 0.5),
            _market(0.4, 0.5, closed=True),
            _market(0.4, 0.5, acceptingOrders=False),
        ],
    }
    # Only one active market remains -> skip
    assert score_event(event) is None


def test_detects_sell_side_arb() -> None:
    # sum(bid) = 1.05 > 1 -> sell-side arb candidate
    event = {
        "id": "e4",
        "title": "ev",
        "slug": "ev",
        "negRisk": True,
        "markets": [_market(0.50, 0.51), _market(0.55, 0.56)],
    }
    sig = score_event(event)
    assert isinstance(sig, EventArbSignal)
    assert sig.n_markets == 2
    assert abs(sig.sum_best_bid - 1.05) < 1e-9
    assert abs(sig.bid_gap - 0.05) < 1e-9
    assert sig.direction == "sell_yes"
    assert is_flagged(sig, fee_buffer=0.02) is True
    assert is_flagged(sig, fee_buffer=0.06) is False


def test_detects_buy_side_arb() -> None:
    # sum(ask) = 0.95 < 1 -> buy-side arb candidate
    event = {
        "id": "e5",
        "title": "ev",
        "slug": "ev",
        "negRisk": True,
        "markets": [_market(0.45, 0.46), _market(0.48, 0.49)],
    }
    sig = score_event(event)
    assert isinstance(sig, EventArbSignal)
    assert abs(sig.sum_best_ask - 0.95) < 1e-9
    assert abs(sig.ask_gap - 0.05) < 1e-9
    assert sig.direction == "buy_yes"
    assert is_flagged(sig, fee_buffer=0.02) is True


def test_no_flag_inside_fee_buffer() -> None:
    event = {
        "id": "e6",
        "title": "ev",
        "slug": "ev",
        "negRisk": True,
        "markets": [_market(0.49, 0.50), _market(0.50, 0.51)],
    }
    sig = score_event(event)
    assert sig is not None
    # sum_bid = 0.99, sum_ask = 1.01 -> both gaps |0.01| < 0.02
    assert is_flagged(sig, fee_buffer=0.02) is False
    assert sig.direction == "none"


def test_records_neg_risk_other_flag() -> None:
    event = {
        "id": "e7",
        "title": "ev",
        "slug": "ev",
        "negRisk": True,
        "markets": [
            _market(0.30, 0.31),
            _market(0.30, 0.31, negRiskOther=True),
        ],
    }
    sig = score_event(event)
    assert sig is not None
    assert sig.has_neg_risk_other is True


def test_real_world_shape_from_probe() -> None:
    # Reproduces the Harvey Weinstein event shape we found during API probing:
    # 6 mutually-exclusive markets, sum(bid)=1.008, sum(ask)=1.033.
    event = {
        "id": "real1",
        "title": "Harvey Weinstein prison time?",
        "slug": "harvey-weinstein-prison-time",
        "negRisk": True,
        "markets": [
            _market(0.777, 0.778),
            _market(0.042, 0.043),
            _market(0.013, 0.025),
            _market(0.049, 0.050),
            _market(0.086, 0.095),
            _market(0.041, 0.042),
        ],
    }
    sig = score_event(event)
    assert sig is not None
    assert sig.n_markets == 6
    assert abs(sig.sum_best_bid - 1.008) < 1e-3
    assert abs(sig.sum_best_ask - 1.033) < 1e-3
    # Sell-side gap = 0.008, below 2% fee buffer
    assert is_flagged(sig, fee_buffer=0.02) is False
    # ...but flaggable at a 50bp buffer
    assert is_flagged(sig, fee_buffer=0.005) is True
