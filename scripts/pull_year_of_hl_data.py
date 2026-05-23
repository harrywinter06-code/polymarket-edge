# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "httpx>=0.28",
# ]
# ///
"""Pull a year of Hyperliquid funding + perp candle data, chunked so we don't
hit the server's per-request cap.

Run via:
    PYTHONPATH=src uv run --script scripts/pull_year_of_hl_data.py

Hyperliquid `fundingHistory` and `candleSnapshot` both 500 on >~30d requests
in our experience; we chunk to 7-day windows with 0.4s spacing.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from datetime import UTC, datetime

import httpx

HL_INFO = "https://api.hyperliquid.xyz/info"
CHUNK_DAYS = 7
SPACING_S = 0.4


async def fetch_chunked(
    client: httpx.AsyncClient,
    *,
    body_template: dict,
    start_ms: int,
    end_ms: int,
    chunk_ms: int,
    label: str,
) -> list[dict]:
    """Call HL /info repeatedly over chunks of `chunk_ms`. Body must have a
    `req` field with startTime/endTime to be overridden."""
    out: list[dict] = []
    cursor = start_ms
    while cursor < end_ms:
        nxt = min(cursor + chunk_ms, end_ms)
        body = {**body_template}
        if "req" in body:
            body["req"] = {**body["req"], "startTime": cursor, "endTime": nxt}
        else:
            body["startTime"] = cursor
            body["endTime"] = nxt
        for retry in range(3):
            try:
                r = await client.post(HL_INFO, json=body, timeout=30.0)
                if r.status_code == 200:
                    j = r.json()
                    if isinstance(j, list):
                        out.extend(j)
                    break
                if r.status_code >= 500:
                    await asyncio.sleep(2.0 * (retry + 1))
                    continue
                print(f"  {label}: HTTP {r.status_code} on {cursor}->{nxt}", file=sys.stderr)
                break
            except httpx.HTTPError as e:
                print(f"  {label}: {e!r} on {cursor}->{nxt}", file=sys.stderr)
                await asyncio.sleep(2.0 * (retry + 1))
        cursor = nxt
        await asyncio.sleep(SPACING_S)
    return out


async def fetch_funding_for_coin(client: httpx.AsyncClient, coin: str, days: int) -> list[dict]:
    end_ms = int(datetime.now(UTC).timestamp() * 1000)
    start_ms = end_ms - days * 86_400_000
    return await fetch_chunked(
        client,
        body_template={"type": "fundingHistory", "coin": coin},
        start_ms=start_ms,
        end_ms=end_ms,
        chunk_ms=CHUNK_DAYS * 86_400_000,
        label=f"funding:{coin}",
    )


async def fetch_candles_for_coin(client: httpx.AsyncClient, coin: str, days: int) -> list[dict]:
    end_ms = int(datetime.now(UTC).timestamp() * 1000)
    start_ms = end_ms - days * 86_400_000
    return await fetch_chunked(
        client,
        body_template={
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": "1h"},
        },
        start_ms=start_ms,
        end_ms=end_ms,
        chunk_ms=CHUNK_DAYS * 86_400_000,
        label=f"candles:{coin}",
    )


def init_candle_table(conn: sqlite3.Connection) -> None:
    # Match the existing (coin, t, close, fetched_at) schema set up by Plan D's
    # hl_extremes.py — no DDL drift.
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS hl_perp_candles (
            coin TEXT NOT NULL,
            t INTEGER NOT NULL,
            close REAL NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (coin, t)
        );
        """
    )
    conn.commit()


def insert_candles(conn: sqlite3.Connection, coin: str, rows: list[dict], fetched_at: str) -> int:
    n = 0
    for r in rows:
        try:
            t = int(r["t"])
            close = float(r["c"])
        except (KeyError, ValueError, TypeError):
            continue
        conn.execute(
            "INSERT OR REPLACE INTO hl_perp_candles (coin, t, close, fetched_at) "
            "VALUES (?,?,?,?)",
            (coin, t, close, fetched_at),
        )
        n += 1
    return n


def insert_funding(conn: sqlite3.Connection, coin: str, rows: list[dict], fetched_at: str) -> int:
    n = 0
    for r in rows:
        try:
            t = int(r["time"])
            funding = float(r["fundingRate"])
            premium = float(r["premium"]) if r.get("premium") is not None else None
        except (KeyError, ValueError, TypeError):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO hl_funding_history "
            "(coin, t, funding, premium, fetched_at) VALUES (?,?,?,?,?)",
            (coin, t, funding, premium, fetched_at),
        )
        n += 1
    return n


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--coins", required=True, help="Comma-sep coin list")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--db", default="polymarket_edge.db")
    args = parser.parse_args()

    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    print(f"pulling {args.days}d for {len(coins)} coins", file=sys.stderr)

    conn = sqlite3.connect(args.db)
    init_candle_table(conn)

    fetched_at = datetime.now(UTC).isoformat()
    total_funding = 0
    total_candles = 0
    async with httpx.AsyncClient() as client:
        for coin in coins:
            print(f"  {coin}...", file=sys.stderr)
            funding_rows = await fetch_funding_for_coin(client, coin, args.days)
            n_f = insert_funding(conn, coin, funding_rows, fetched_at)
            total_funding += n_f
            print(f"    funding: {len(funding_rows)} pulled, {n_f} new", file=sys.stderr)

            candle_rows = await fetch_candles_for_coin(client, coin, args.days)
            n_c = insert_candles(conn, coin, candle_rows, fetched_at)
            total_candles += n_c
            print(f"    candles: {len(candle_rows)} pulled, {n_c} new", file=sys.stderr)
            conn.commit()

    print(f"\nTotal: {total_funding} funding rows, {total_candles} candle rows inserted")
    print(f"DB: {args.db}")


if __name__ == "__main__":
    asyncio.run(main())
