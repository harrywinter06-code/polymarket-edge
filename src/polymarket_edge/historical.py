"""Polymarket CLOB historical price-series fetcher.

CLOB `/prices-history` is constrained to >=12h granularity on resolved markets
(https://github.com/Polymarket/py-clob-client/issues/216), so this module is
useful for long-window magnitude/persistence analysis but cannot reconstruct
the fine-grained orderbook required to simulate execution P&L on resolved
events. For execution-realistic measurement we rely on live forward observation
(see :mod:`monitor`).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

CLOB_BASE = "https://clob.polymarket.com"
RATE_LIMIT_SECONDS = 0.5
DEFAULT_TIMEOUT = 20.0


async def fetch_prices_history(
    client: httpx.AsyncClient,
    token_id: str,
    *,
    interval: str = "all",
    fidelity_minutes: int = 60,
) -> list[dict[str, Any]]:
    """Return the price history for a single CLOB token.

    Each entry: {"t": <unix seconds>, "p": <price>}.
    `interval` is a duration string ('all', '1m', '1w', '1d', '6h', '1h').
    `fidelity_minutes` is the minutes per bucket; CLOB silently rounds up to
    its 12h floor for resolved markets.
    """
    params = {
        "market": token_id,
        "interval": interval,
        "fidelity": str(fidelity_minutes),
    }
    r = await client.get(f"{CLOB_BASE}/prices-history", params=params)
    r.raise_for_status()
    payload = r.json()
    return payload.get("history", []) if isinstance(payload, dict) else []


async def fetch_prices_for_tokens(
    token_ids: list[str],
    *,
    interval: str = "all",
    fidelity_minutes: int = 60,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, list[dict[str, Any]]]:
    """Sequentially fetch price history for many tokens. Rate-limited."""
    out: dict[str, list[dict[str, Any]]] = {}
    async with httpx.AsyncClient(timeout=timeout) as client:
        for tid in token_ids:
            out[tid] = await fetch_prices_history(
                client, tid, interval=interval, fidelity_minutes=fidelity_minutes
            )
            await asyncio.sleep(RATE_LIMIT_SECONDS)
    return out
