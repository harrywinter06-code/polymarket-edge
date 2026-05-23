"""Tests for the Hyperliquid info-endpoint fetcher and persistence helpers."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx

from polymarket_edge import hyperliquid


def test_annualize_multiplies_by_hours_per_year() -> None:
    assert hyperliquid.annualize(0.0001) == 0.0001 * hyperliquid.HOURS_PER_YEAR


def test_now_ms_and_now_iso_are_consistent() -> None:
    ms = hyperliquid.now_ms()
    assert ms > 1_700_000_000_000  # post-2023
    iso = hyperliquid.now_iso()
    assert "T" in iso


def test_safe_float_handles_none_numeric_invalid() -> None:
    assert hyperliquid._safe_float(None) is None
    assert hyperliquid._safe_float("1.5") == 1.5
    assert hyperliquid._safe_float("nope") is None
    assert hyperliquid._safe_float([1, 2]) is None


def test_fetch_meta_and_ctxs_unpacks_universe_and_contexts(mock_http) -> None:
    universe = [{"name": "BTC", "szDecimals": 4, "maxLeverage": 50, "marginTableId": 1}]
    ctxs = [{"funding": "0.0001", "markPx": "60000", "openInterest": "1000"}]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == {"type": "metaAndAssetCtxs"}
        return httpx.Response(200, content=json.dumps([{"universe": universe}, ctxs]).encode())

    mock_http(handler)
    u, c = asyncio.run(hyperliquid.fetch_meta_and_ctxs())
    assert u == universe
    assert c == ctxs


def test_fetch_funding_history_returns_list_or_empty(mock_http) -> None:
    rows = [{"coin": "BTC", "fundingRate": "0.0001", "premium": "0", "time": 1_700_000_000_000}]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["type"] == "fundingHistory"
        assert body["coin"] == "BTC"
        return httpx.Response(200, content=json.dumps(rows).encode())

    mock_http(handler)

    async def runner() -> list:
        async with httpx.AsyncClient() as client:
            return await hyperliquid.fetch_funding_history(
                client, "BTC", start_ms=1_700_000_000_000
            )

    out = asyncio.run(runner())
    assert out == rows


def test_fetch_funding_history_handles_non_list_payload(mock_http) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"unexpected": "shape"}')

    mock_http(handler)

    async def runner() -> list:
        async with httpx.AsyncClient() as client:
            return await hyperliquid.fetch_funding_history(client, "BTC", start_ms=0)

    assert asyncio.run(runner()) == []


def test_fetch_funding_history_chunked_walks_full_window(mock_http) -> None:
    """A wider-than-chunk window is split into chunk_days slices; each call's
    payload is concatenated. Verifies the fix for the 500-row HL API cap."""
    chunk_days = 7
    window_days = 21  # exactly 3 chunks
    start_ms = 1_700_000_000_000
    end_ms = start_ms + window_days * 86_400 * 1000

    chunk_starts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        chunk_starts.append(int(body["startTime"]))
        # Return 168 hourly rows per 7-day chunk.
        t0 = int(body["startTime"])
        rows = [
            {"coin": body["coin"], "fundingRate": "0.0001", "time": t0 + i * 3_600_000}
            for i in range(168)
        ]
        return httpx.Response(200, content=json.dumps(rows).encode())

    mock_http(handler)

    async def runner() -> list:
        async with httpx.AsyncClient() as client:
            return await hyperliquid.fetch_funding_history_chunked(
                client, "BTC",
                start_ms=start_ms, end_ms=end_ms,
                chunk_days=chunk_days, rate_limit_seconds=0.0,
            )

    rows = asyncio.run(runner())
    assert len(rows) == 168 * 3
    # Three chunk starts at 0, 7d, 14d (in ms).
    expected_starts = [start_ms + i * chunk_days * 86_400 * 1000 for i in range(3)]
    assert chunk_starts == expected_starts


def test_fetch_funding_history_chunked_handles_partial_final_chunk(mock_http) -> None:
    """Last chunk's endTime is the window end, not start + chunk_days."""
    chunk_days = 7
    start_ms = 1_700_000_000_000
    end_ms = start_ms + 10 * 86_400 * 1000  # 10 days -> 2 chunks (7d + 3d partial)
    seen: list[tuple[int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((int(body["startTime"]), int(body["endTime"])))
        return httpx.Response(200, content=b"[]")

    mock_http(handler)

    async def runner() -> list:
        async with httpx.AsyncClient() as client:
            return await hyperliquid.fetch_funding_history_chunked(
                client, "BTC",
                start_ms=start_ms, end_ms=end_ms,
                chunk_days=chunk_days, rate_limit_seconds=0.0,
            )

    asyncio.run(runner())
    assert len(seen) == 2
    # Final chunk's end is the original window end, not start+chunk.
    assert seen[-1][1] == end_ms


def test_fetch_funding_history_many_iterates_each_coin(mock_http) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body["coin"])
        return httpx.Response(
            200,
            content=json.dumps(
                [{"coin": body["coin"], "fundingRate": "0.0001", "time": 1}]
            ).encode(),
        )

    mock_http(handler)
    result = asyncio.run(hyperliquid.fetch_funding_history_many(["BTC", "ETH"], days=1))
    assert seen == ["BTC", "ETH"]
    assert set(result.keys()) == {"BTC", "ETH"}


def test_upsert_universe_inserts_one_row_per_coin(tmp_conn: sqlite3.Connection) -> None:
    universe = [
        {"name": "BTC", "szDecimals": 4, "maxLeverage": 50, "marginTableId": 1},
        {"name": "ETH", "szDecimals": 4, "maxLeverage": 50, "marginTableId": 1},
    ]
    hyperliquid.upsert_universe(tmp_conn, universe, "2026-01-01T00:00:00+00:00")
    rows = tmp_conn.execute("SELECT coin FROM hl_universe ORDER BY coin").fetchall()
    assert [r[0] for r in rows] == ["BTC", "ETH"]


def test_upsert_universe_is_idempotent(tmp_conn: sqlite3.Connection) -> None:
    universe = [{"name": "BTC", "szDecimals": 4, "maxLeverage": 50, "marginTableId": 1}]
    hyperliquid.upsert_universe(tmp_conn, universe, "2026-01-01T00:00:00+00:00")
    hyperliquid.upsert_universe(tmp_conn, universe, "2026-01-02T00:00:00+00:00")
    rows = tmp_conn.execute("SELECT * FROM hl_universe").fetchall()
    assert len(rows) == 1
    assert rows[0]["fetched_at"] == "2026-01-02T00:00:00+00:00"


def test_insert_funding_snapshot_persists_floats(tmp_conn: sqlite3.Connection) -> None:
    ctx = {
        "funding": "0.0001",
        "markPx": "60000",
        "midPx": "60001",
        "oraclePx": "60002",
        "premium": "0.0",
        "openInterest": "1000",
        "dayNtlVlm": "5000",
    }
    hyperliquid.insert_funding_snapshot(
        tmp_conn, coin="BTC", ctx=ctx, snapshot_at="2026-01-01T00:00:00+00:00"
    )
    row = tmp_conn.execute("SELECT * FROM hl_funding_snapshots").fetchone()
    assert row["coin"] == "BTC"
    assert row["funding"] == 0.0001
    assert row["mark_px"] == 60000.0
    assert row["open_interest"] == 1000.0


def test_insert_funding_history_counts_ok_and_malformed(tmp_conn: sqlite3.Connection) -> None:
    rows = [
        {"coin": "BTC", "fundingRate": "0.0001", "premium": "0", "time": 1_700_000_000_000},
        {"coin": "BTC", "fundingRate": "garbage", "premium": "0", "time": 1_700_003_600_000},
        {"coin": "BTC", "premium": "0", "time": 1_700_007_200_000},  # missing fundingRate
        {"coin": "BTC", "fundingRate": "0.0002", "time": 1_700_010_800_000},
    ]
    n_ok, n_bad = hyperliquid.insert_funding_history(
        tmp_conn, "BTC", rows, "2026-01-01T00:00:00+00:00"
    )
    assert n_ok == 2
    assert n_bad == 2
    persisted = tmp_conn.execute(
        "SELECT COUNT(*) FROM hl_funding_history WHERE coin='BTC'"
    ).fetchone()[0]
    assert persisted == 2


def test_insert_funding_history_dedupes_on_unique_constraint(
    tmp_conn: sqlite3.Connection,
) -> None:
    row = {"coin": "BTC", "fundingRate": "0.0001", "time": 1_700_000_000_000}
    # INSERT OR IGNORE: the second call inserts nothing but still counts as ok-parsed.
    n1, _ = hyperliquid.insert_funding_history(tmp_conn, "BTC", [row], "t0")
    n2, _ = hyperliquid.insert_funding_history(tmp_conn, "BTC", [row], "t1")
    assert n1 == 1 and n2 == 1
    persisted = tmp_conn.execute(
        "SELECT COUNT(*) FROM hl_funding_history WHERE coin='BTC'"
    ).fetchone()[0]
    assert persisted == 1
