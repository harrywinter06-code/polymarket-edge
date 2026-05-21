"""Polymarket gamma API ingestion."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx

GAMMA_BASE = "https://gamma-api.polymarket.com"
# Large pages (500) cause MemoryError on the embedded-markets payload; 50 is
# safe and the rate limiter still keeps us well under 60 req/min.
PAGE_SIZE = 50
RATE_LIMIT_SECONDS = 1.2
DEFAULT_TIMEOUT = 30.0


async def fetch_all_active_events(
    *,
    max_events: int | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Paginate /events and return all currently active, non-closed events.

    Each event embeds its constituent markets in the response — no extra
    per-event calls needed for the day-1 scope.
    """
    events: list[dict[str, Any]] = []
    offset = 0
    async with httpx.AsyncClient(timeout=timeout) as client:
        while True:
            params = {
                "limit": PAGE_SIZE,
                "offset": offset,
                "active": "true",
                "closed": "false",
            }
            r = await client.get(f"{GAMMA_BASE}/events", params=params)
            r.raise_for_status()
            page = r.json()
            if not page:
                break
            events.extend(page)
            if max_events is not None and len(events) >= max_events:
                return events[:max_events]
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
            await asyncio.sleep(RATE_LIMIT_SECONDS)
    return events


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
