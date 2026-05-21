"""Hyperliquid info-endpoint fetcher.

Single endpoint: POST https://api.hyperliquid.xyz/info with a JSON `type` field.
- type=metaAndAssetCtxs returns [universe, asset_ctxs] for all perps
- type=fundingHistory returns the hourly funding rate series for one coin

Funding on Hyperliquid is paid every hour. The `funding` field on the asset
context is the rate for the NEXT funding payment; `fundingHistory` returns
realized historical rates with timestamps in milliseconds.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from typing import Any

import httpx

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
DEFAULT_TIMEOUT = 30.0
RATE_LIMIT_SECONDS = 0.2
HOURS_PER_YEAR = 24 * 365


def now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def fetch_meta_and_ctxs(
    *, timeout: float = DEFAULT_TIMEOUT
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (universe, asset_contexts). Both are parallel-indexed by coin."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(HL_INFO_URL, json={"type": "metaAndAssetCtxs"})
        r.raise_for_status()
        d = r.json()
    return d[0]["universe"], d[1]


async def fetch_funding_history(
    client: httpx.AsyncClient,
    coin: str,
    *,
    start_ms: int,
    end_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Return historical funding rows: [{coin, fundingRate, premium, time}, ...]."""
    body: dict[str, Any] = {"type": "fundingHistory", "coin": coin, "startTime": start_ms}
    if end_ms is not None:
        body["endTime"] = end_ms
    r = await client.post(HL_INFO_URL, json=body)
    r.raise_for_status()
    out = r.json()
    return out if isinstance(out, list) else []


async def fetch_funding_history_many(
    coins: list[str],
    *,
    days: int,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, list[dict[str, Any]]]:
    end_ms = now_ms()
    start_ms = end_ms - days * 86_400 * 1000
    out: dict[str, list[dict[str, Any]]] = {}
    async with httpx.AsyncClient(timeout=timeout) as client:
        for coin in coins:
            out[coin] = await fetch_funding_history(client, coin, start_ms=start_ms, end_ms=end_ms)
            await asyncio.sleep(RATE_LIMIT_SECONDS)
    return out


def annualize(hourly_rate: float) -> float:
    """Convert an hourly funding rate (e.g. 0.0000125) to annualized (4*365=1095% scenario)."""
    return hourly_rate * HOURS_PER_YEAR


def upsert_universe(
    conn: sqlite3.Connection,
    universe: list[dict[str, Any]],
    fetched_at: str,
) -> None:
    for u in universe:
        conn.execute(
            """
            INSERT OR REPLACE INTO hl_universe
            (coin, sz_decimals, max_leverage, margin_table_id, fetched_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                u.get("name"),
                u.get("szDecimals"),
                u.get("maxLeverage"),
                u.get("marginTableId"),
                fetched_at,
            ),
        )


def insert_funding_snapshot(
    conn: sqlite3.Connection,
    *,
    coin: str,
    ctx: dict[str, Any],
    snapshot_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO hl_funding_snapshots
        (coin, funding, mark_px, mid_px, oracle_px, premium, open_interest,
         day_ntl_vlm, snapshot_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            coin,
            float(ctx["funding"]),
            _safe_float(ctx.get("markPx")),
            _safe_float(ctx.get("midPx")),
            _safe_float(ctx.get("oraclePx")),
            _safe_float(ctx.get("premium")),
            _safe_float(ctx.get("openInterest")),
            _safe_float(ctx.get("dayNtlVlm")),
            snapshot_at,
        ),
    )


def insert_funding_history(
    conn: sqlite3.Connection,
    coin: str,
    rows: list[dict[str, Any]],
    fetched_at: str,
) -> int:
    n = 0
    for r in rows:
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO hl_funding_history
                (coin, t, funding, premium, fetched_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    coin,
                    int(r["time"]),
                    float(r["fundingRate"]),
                    _safe_float(r.get("premium")),
                    fetched_at,
                ),
            )
            n += 1
        except (KeyError, ValueError, TypeError):
            continue
    return n


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
