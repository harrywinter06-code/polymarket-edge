"""Tests for the Polymarket gamma API ingestion layer."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest

from polymarket_edge import fetch


def _make_pages(pages: list[list[dict]]) -> Callable[[httpx.Request], httpx.Response]:
    """Return a handler that walks `pages` keyed by `offset` query param."""

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", "0"))
        idx = offset // fetch.PAGE_SIZE
        page = pages[idx] if idx < len(pages) else []
        return httpx.Response(200, content=json.dumps(page).encode())

    return handler


def test_now_iso_is_parseable_and_utc() -> None:
    from datetime import datetime

    s = fetch.now_iso()
    parsed = datetime.fromisoformat(s)
    assert parsed.tzinfo is not None


def test_fetch_all_terminates_on_short_page(mock_http, monkeypatch) -> None:
    # Single page with fewer items than PAGE_SIZE terminates immediately.
    monkeypatch.setattr(fetch, "PAGE_SIZE", 50)
    page = [{"id": str(i)} for i in range(3)]
    mock_http(_make_pages([page]))
    result = asyncio.run(fetch.fetch_all_active_events())
    assert len(result) == 3


def test_fetch_all_paginates_to_empty_terminator(mock_http, monkeypatch) -> None:
    monkeypatch.setattr(fetch, "PAGE_SIZE", 2)
    pages = [
        [{"id": "1"}, {"id": "2"}],
        [{"id": "3"}, {"id": "4"}],
        [],  # terminator
    ]
    mock_http(_make_pages(pages))
    result = asyncio.run(fetch.fetch_all_active_events())
    assert [e["id"] for e in result] == ["1", "2", "3", "4"]


def test_fetch_all_respects_max_events_cap(mock_http, monkeypatch) -> None:
    monkeypatch.setattr(fetch, "PAGE_SIZE", 5)
    pages = [[{"id": str(i)} for i in range(5)], [{"id": str(i)} for i in range(5, 10)]]
    mock_http(_make_pages(pages))
    result = asyncio.run(fetch.fetch_all_active_events(max_events=3))
    assert len(result) == 3
    assert [e["id"] for e in result] == ["0", "1", "2"]


def test_fetch_all_raises_on_http_error(mock_http) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"{}")

    mock_http(handler)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(fetch.fetch_all_active_events())


def test_fetch_all_sends_active_and_closed_query_params(mock_http) -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, content=b"[]")

    mock_http(handler)
    asyncio.run(fetch.fetch_all_active_events())
    assert seen and seen[0]["active"] == "true"
    assert seen[0]["closed"] == "false"
