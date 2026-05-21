"""Cross-venue lead-lag analysis: Polymarket prediction-market price vs Hyperliquid perp mark.

The Polymarket leg uses the existing CLOB `/prices-history` endpoint. Active markets
return finer-than-12h granularity (verified: hourly fidelity on the live
`how-many-fed-rate-cuts-in-2026` event); resolved markets are floored at 12h per
py-clob-client#216. To keep the method honest against the worst-case PM granularity,
we bucket both legs to 12h before correlating.

The Hyperliquid leg pulls hourly OHLC mark via `candleSnapshot` (close = mark).
Forward-fill within a bucket if HL data is sparse; no backward-fill (would leak future).

Lead-lag convention: positive lag means PM leads HL, i.e. corr(pm[t], hl[t+lag]) — a
positive value at lag=+k says today's PM move predicts HL's move k buckets later.
"""

from __future__ import annotations

import asyncio
import math
import sqlite3
from dataclasses import dataclass
from typing import Any

import httpx

from .historical import fetch_prices_history
from .hyperliquid import HL_INFO_URL

DEFAULT_TIMEOUT = 30.0
BUCKET_MS_12H = 12 * 3_600_000
SECONDS_PER_HOUR = 3600


@dataclass(frozen=True)
class AlignedRow:
    """One 12h bucket with PM price, HL mark, and per-bucket changes."""

    t_ms: int
    pm_price: float
    hl_mark: float
    pm_delta: float       # raw PM price change since previous bucket (in probability units)
    hl_log_return: float  # log(hl_t / hl_{t-1})


async def fetch_pm_price_history(
    token_id: str,
    *,
    days: int = 30,
    fidelity_minutes: int = 60,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[tuple[int, float]]:
    """Return (unix_seconds, price) pairs for a Polymarket CLOB token.

    Uses `interval='1m'` (one month, ~30 days) when days<=31, else `'all'`. For
    resolved markets the CLOB silently floors fidelity to 12h regardless of the
    requested value; we accept that and bucket downstream.
    """
    interval = "1m" if days <= 31 else "all"
    async with httpx.AsyncClient(timeout=timeout) as client:
        rows = await fetch_prices_history(
            client, token_id, interval=interval, fidelity_minutes=fidelity_minutes
        )
    cutoff_s = _now_s() - days * 86_400
    return [(int(r["t"]), float(r["p"])) for r in rows if int(r["t"]) >= cutoff_s]


async def fetch_hl_mark_history(
    coin: str,
    *,
    days: int = 30,
    interval: str = "1h",
    timeout: float = DEFAULT_TIMEOUT,
) -> list[tuple[int, float]]:
    """Return (unix_ms, close_price) for a Hyperliquid perp via candleSnapshot.

    Close-of-candle is used as the mark proxy. Hyperliquid's `markPx` is only
    exposed live via metaAndAssetCtxs; the close of the hourly candle is the
    standard backtest-grade substitute and is what the funding mechanism marks
    against in practice.
    """
    end_ms = _now_ms()
    start_ms = end_ms - days * 86_400 * 1000
    body: dict[str, Any] = {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms},
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(HL_INFO_URL, json=body)
        r.raise_for_status()
        candles = r.json()
    if not isinstance(candles, list):
        return []
    return [(int(c["t"]), float(c["c"])) for c in candles if "t" in c and "c" in c]


def align_series(
    pm_history: list[tuple[int, float]],
    hl_history: list[tuple[int, float]],
    *,
    bucket_minutes: int = 720,
) -> list[AlignedRow]:
    """Bucket both legs to a common time grid and compute per-bucket changes.

    PM input timestamps are seconds; HL input timestamps are milliseconds. The
    returned `t_ms` is the bucket-start in milliseconds. Within each bucket we
    take the *last* observation (closest to bucket close) — for the PM leg this
    is the bucket-close probability, for the HL leg it is the candle close.

    Buckets are aligned to absolute epoch (i.e. floor(t / bucket_ms) * bucket_ms),
    not to the first observation, so two independent runs produce identical grids.

    Buckets without coverage on *both* legs are dropped. The very first surviving
    bucket has `pm_delta` and `hl_log_return` set from the prior available bucket
    where possible; if no prior bucket exists they default to 0.0.

    Raises ValueError if the unit-of-timestamp assumption is violated — a
    common foot-gun is feeding both inputs in the same unit. A 2026-era PM
    timestamp in seconds is ~1.7e9, in milliseconds ~1.7e12; we use 1e11 as
    the discriminator (Unix epoch ms crossed that around 2001 in seconds and
    around the year 5138 in seconds, so the boundary is comfortable).
    """
    if bucket_minutes <= 0:
        raise ValueError("bucket_minutes must be positive")
    _validate_timestamp_units(pm_history, expected="seconds", name="pm_history")
    _validate_timestamp_units(hl_history, expected="milliseconds", name="hl_history")
    bucket_ms = bucket_minutes * 60 * 1000

    pm_by_bucket = _bucket_last(((t * 1000, p) for t, p in pm_history), bucket_ms)
    hl_by_bucket = _bucket_last(hl_history, bucket_ms)

    common = sorted(set(pm_by_bucket) & set(hl_by_bucket))
    rows: list[AlignedRow] = []
    prev_pm: float | None = None
    prev_hl: float | None = None
    for t_ms in common:
        pm = pm_by_bucket[t_ms]
        hl = hl_by_bucket[t_ms]
        pm_delta = 0.0 if prev_pm is None else pm - prev_pm
        if prev_hl is None or prev_hl <= 0 or hl <= 0:
            hl_log_return = 0.0
        else:
            hl_log_return = math.log(hl / prev_hl)
        rows.append(
            AlignedRow(t_ms=t_ms, pm_price=pm, hl_mark=hl,
                       pm_delta=pm_delta, hl_log_return=hl_log_return)
        )
        prev_pm = pm
        prev_hl = hl
    return rows


def compute_lead_lag(
    aligned: list[AlignedRow],
    *,
    max_lag_buckets: int = 4,
) -> dict[int, float]:
    """Return {lag: Pearson correlation} for lag in [-max_lag, +max_lag] buckets.

    Convention: positive lag means PM leads HL. We compute
        corr( pm_delta[: N-lag], hl_log_return[lag :] )    for lag >= 0
        corr( pm_delta[-lag :], hl_log_return[: N+lag] )   for lag <  0

    Lag=+1 with positive correlation -> a PM probability jump up at time t is
    followed by an HL up-move at time t+1 (PM leads HL).
    Lag=-1 with positive correlation -> an HL up-move at time t is followed
    by a PM probability jump at t+1 (HL leads PM).

    Returns NaN for any lag where fewer than 3 overlapping pairs exist (Pearson
    on N<3 is undefined / numerically meaningless). Drops the very first row of
    each series because its pm_delta/hl_log_return are seeded to 0.0.
    """
    if max_lag_buckets < 0:
        raise ValueError("max_lag_buckets must be non-negative")
    pm_d = [r.pm_delta for r in aligned[1:]]
    hl_r = [r.hl_log_return for r in aligned[1:]]
    n = len(pm_d)
    out: dict[int, float] = {}
    for lag in range(-max_lag_buckets, max_lag_buckets + 1):
        if lag >= 0:
            x = pm_d[: n - lag] if lag > 0 else pm_d
            y = hl_r[lag:] if lag > 0 else hl_r
        else:
            x = pm_d[-lag:]
            y = hl_r[: n + lag]
        out[lag] = _pearson(x, y)
    return out


_UNIT_BOUNDARY = 10**11  # ~year 5138 in seconds, year 1973 in milliseconds


def _validate_timestamp_units(
    series: list[tuple[int, float]], *, expected: str, name: str
) -> None:
    """Cheap guard against feeding the wrong timestamp unit.

    `expected` is "seconds" or "milliseconds". The check inspects the median-ish
    timestamp (just the first one for speed) and complains if it falls on the
    wrong side of 1e11. A 2026 timestamp is ~1.7e9 in seconds, ~1.7e12 in ms.
    """
    if not series:
        return
    sample = series[0][0]
    if expected == "seconds" and sample >= _UNIT_BOUNDARY:
        raise ValueError(
            f"{name}: timestamp {sample} looks like milliseconds, "
            f"but this function expects seconds for the PM leg"
        )
    if expected == "milliseconds" and sample < _UNIT_BOUNDARY:
        raise ValueError(
            f"{name}: timestamp {sample} looks like seconds, "
            f"but this function expects milliseconds for the HL leg"
        )


def _pearson(x: list[float], y: list[float]) -> float:
    """Pearson correlation. Returns float('nan') if undefined (N<3 or zero variance)."""
    n = len(x)
    if n != len(y) or n < 3:
        return float("nan")
    mx = sum(x) / n
    my = sum(y) / n
    sx2 = sum((xi - mx) ** 2 for xi in x)
    sy2 = sum((yi - my) ** 2 for yi in y)
    if sx2 == 0 or sy2 == 0:
        return float("nan")
    sxy = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y, strict=True))
    return sxy / math.sqrt(sx2 * sy2)


def _bucket_last(items: Any, bucket_ms: int) -> dict[int, float]:
    """Bucket (ts_ms, value) pairs to absolute-epoch buckets, taking the last
    observation within each bucket (highest timestamp wins)."""
    buckets: dict[int, tuple[int, float]] = {}
    for t_ms, v in items:
        b = (t_ms // bucket_ms) * bucket_ms
        prev = buckets.get(b)
        if prev is None or t_ms > prev[0]:
            buckets[b] = (t_ms, v)
    return {b: v for b, (_, v) in buckets.items()}


def insert_aligned_rows(
    conn: sqlite3.Connection,
    *,
    pm_token_id: str,
    hl_coin: str,
    rows: list[AlignedRow],
    fetched_at: str,
) -> int:
    """Persist aligned rows to `cross_venue_aligned`. Returns rows inserted."""
    n = 0
    for r in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO cross_venue_aligned
            (pm_token_id, hl_coin, t_ms, pm_price, hl_mark, pm_delta, hl_log_return, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (pm_token_id, hl_coin, r.t_ms, r.pm_price, r.hl_mark,
             r.pm_delta, r.hl_log_return, fetched_at),
        )
        n += 1
    return n


def _now_s() -> int:
    from datetime import UTC, datetime
    return int(datetime.now(UTC).timestamp())


def _now_ms() -> int:
    return _now_s() * 1000


async def run_pair(
    *,
    pm_token_id: str,
    hl_coin: str,
    days: int,
    bucket_minutes: int = 720,
    max_lag_buckets: int = 4,
) -> tuple[list[AlignedRow], dict[int, float]]:
    """Convenience: fetch both legs, align, compute lead-lag. Returns (rows, lags)."""
    pm_task = asyncio.create_task(fetch_pm_price_history(pm_token_id, days=days))
    hl_task = asyncio.create_task(fetch_hl_mark_history(hl_coin, days=days))
    pm = await pm_task
    hl = await hl_task
    rows = align_series(pm, hl, bucket_minutes=bucket_minutes)
    lags = compute_lead_lag(rows, max_lag_buckets=max_lag_buckets)
    return rows, lags
