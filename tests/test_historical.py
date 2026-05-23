"""Tests for the CLOB /prices-history fetcher."""

from __future__ import annotations

import asyncio
import json

import httpx

from polymarket_edge import historical


def test_fetch_prices_history_returns_payload_history_list(mock_http) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "market" in request.url.params
        return httpx.Response(
            200,
            content=json.dumps(
                {"history": [{"t": 1, "p": 0.5}, {"t": 2, "p": 0.6}]}
            ).encode(),
        )

    mock_http(handler)

    async def runner() -> list[dict]:
        async with httpx.AsyncClient() as client:
            return await historical.fetch_prices_history(client, "tok-1")

    rows = asyncio.run(runner())
    assert rows == [{"t": 1, "p": 0.5}, {"t": 2, "p": 0.6}]


def test_fetch_prices_history_handles_non_dict_payload(mock_http) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]")  # CLOB sometimes returns raw list

    mock_http(handler)

    async def runner() -> list[dict]:
        async with httpx.AsyncClient() as client:
            return await historical.fetch_prices_history(client, "tok-1")

    assert asyncio.run(runner()) == []


def test_fetch_prices_for_tokens_iterates_each_token(mock_http) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["market"])
        return httpx.Response(
            200,
            content=json.dumps({"history": [{"t": 1, "p": 0.5}]}).encode(),
        )

    mock_http(handler)
    result = asyncio.run(historical.fetch_prices_for_tokens(["a", "b", "c"]))
    assert calls == ["a", "b", "c"]
    assert set(result.keys()) == {"a", "b", "c"}
    for v in result.values():
        assert v == [{"t": 1, "p": 0.5}]


def test_fetch_prices_history_passes_interval_and_fidelity(mock_http) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, content=b'{"history": []}')

    mock_http(handler)

    async def runner() -> None:
        async with httpx.AsyncClient() as client:
            await historical.fetch_prices_history(
                client, "tok-1", interval="6h", fidelity_minutes=120
            )

    asyncio.run(runner())
    assert seen["interval"] == "6h"
    assert seen["fidelity"] == "120"
