"""Shared pytest fixtures and HTTP-mocking helpers."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from polymarket_edge import db

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def tmp_conn(tmp_db_path: Path):
    """Schema-initialised SQLite connection backed by a tmp file."""
    conn = db.connect(tmp_db_path)
    db.init_schema(conn)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Event / market factories
# ---------------------------------------------------------------------------


def make_market(
    market_id: str,
    *,
    best_bid: float | None = 0.5,
    best_ask: float | None = 0.5,
    active: bool = True,
    closed: bool = False,
    accepting_orders: bool = True,
    neg_risk: bool = True,
    neg_risk_other: bool = False,
    yes_token_id: str | None = None,
    no_token_id: str | None = None,
    condition_id: str | None = None,
    question: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    yes = yes_token_id or f"yes-{market_id}"
    no = no_token_id or f"no-{market_id}"
    return {
        "id": market_id,
        "question": question or f"Market {market_id}?",
        "slug": f"m-{market_id}",
        "conditionId": condition_id or f"cond-{market_id}",
        "clobTokenIds": f'["{yes}", "{no}"]',
        "outcomes": '["Yes","No"]',
        "negRisk": neg_risk,
        "negRiskOther": neg_risk_other,
        "active": active,
        "closed": closed,
        "acceptingOrders": accepting_orders,
        "bestBid": best_bid,
        "bestAsk": best_ask,
        "spread": (best_ask or 0) - (best_bid or 0) if best_bid and best_ask else None,
        "lastTradePrice": best_bid,
        "volumeNum": 1000.0,
        "outcomePrices": '["0.5","0.5"]',
        "endDate": "2099-01-01T00:00:00Z",
        "orderMinSize": 5.0,
        "orderPriceMinTickSize": 0.01,
        **extra,
    }


def make_event(
    event_id: str = "evt-1",
    *,
    neg_risk: bool = True,
    neg_risk_augmented: bool = False,
    markets: list[dict[str, Any]] | None = None,
    title: str = "Test event",
    slug: str = "test-event",
    tags: list[dict[str, str]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    if markets is None:
        markets = [
            make_market("m1", best_bid=0.6, best_ask=0.62),
            make_market("m2", best_bid=0.45, best_ask=0.47),
        ]
    return {
        "id": event_id,
        "slug": slug,
        "title": title,
        "negRisk": neg_risk,
        "negRiskAugmented": neg_risk_augmented,
        "endDate": "2099-01-01T00:00:00Z",
        "volume": 100.0,
        "liquidity": 100.0,
        "markets": markets,
        "tags": tags if tags is not None else [{"label": "Sports"}],
        **extra,
    }


@pytest.fixture
def make_event_fixture() -> Callable[..., dict[str, Any]]:
    return make_event


@pytest.fixture
def make_market_fixture() -> Callable[..., dict[str, Any]]:
    return make_market


# ---------------------------------------------------------------------------
# HTTP mocking
# ---------------------------------------------------------------------------


HttpHandler = Callable[[httpx.Request], httpx.Response]


def install_mock_transport(
    monkeypatch: pytest.MonkeyPatch, handler: HttpHandler
) -> None:
    """Replace httpx.AsyncClient so every instance uses a MockTransport.

    Modules under test construct `httpx.AsyncClient(timeout=...)` inline; this
    forces every such client to dispatch through the given handler instead of
    the network.
    """
    real_cls = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_cls(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.fixture
def mock_http(monkeypatch: pytest.MonkeyPatch):
    """Yield a one-arg `install(handler)` callable for tests."""

    def install(handler: HttpHandler) -> None:
        install_mock_transport(monkeypatch, handler)

    return install


@pytest.fixture(autouse=True)
def _zero_rate_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise the module-level rate-limit constants so tests don't sleep.

    Modules read these as module attributes at call time, so monkeypatch works.
    """
    from polymarket_edge import (
        book_depth,
        cross_venue,
        fetch,
        historical,
        hyperliquid,
        polymarket_mm_sim,
    )

    monkeypatch.setattr(fetch, "RATE_LIMIT_SECONDS", 0.0, raising=False)
    monkeypatch.setattr(book_depth, "RATE_LIMIT_SECONDS", 0.0, raising=False)
    monkeypatch.setattr(hyperliquid, "RATE_LIMIT_SECONDS", 0.0, raising=False)
    monkeypatch.setattr(historical, "RATE_LIMIT_SECONDS", 0.0, raising=False)
    monkeypatch.setattr(polymarket_mm_sim, "RATE_LIMIT_SECONDS", 0.0, raising=False)
    # cross_venue doesn't expose a constant, but its run_pair has no sleep.
    _ = cross_venue
