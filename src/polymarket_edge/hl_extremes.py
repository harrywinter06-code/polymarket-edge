"""Hyperliquid funding-extreme directional study (Plan D).

Tests whether perp prices at extreme funding events (|z| > thresh vs trailing
168h) rally or crash over a fixed forward hold horizon. The standard intuition
is "high-funding -> short" (longs paying through the nose, shorts collect
funding while price grinds down); this module quantifies that directly at the
tail of the funding distribution rather than averaging over all positive coins.

Methodology guard-rails:
  - The z-score window is STRICTLY TRAILING: rows[t - 168 : t]. The candidate
    hour `t` is NOT included in its own mean/std. No look-ahead.
  - Each event holds for exactly `hold_hours`. Events whose exit hour falls
    past the data window are dropped (selection-bias mitigation, end-of-window
    truncation).
  - Stats are stdlib-only: t-stat = mean / (std / sqrt(N)), Sharpe annualised
    via sqrt(periods_per_year) with periods_per_year = 8760 / hold_hours.
  - The 18-test family (3 thresholds x 2 directions x 3 horizons) requires
    Bonferroni correction; the consumer script enforces the t > 3.05 bar.

Per-event independence note: without cooldown, two consecutive z>2 hours on
the same coin produce two events that share most of their forward window.
`identify_extreme_events` exposes a `cooldown_hours` parameter; the study
script reports both cooldown=0 (raw) and cooldown=hold_hours (independent)
when the raw event count is high enough to support both.
"""

from __future__ import annotations

import asyncio
import math
import sqlite3
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from .hl_backtest import FundingTick
from .hyperliquid import HL_INFO_URL, now_iso, now_ms

HOUR_MS = 3_600_000
HOURS_PER_YEAR = 24 * 365
TRAILING_HOURS = 168
DEFAULT_TIMEOUT = 30.0
RATE_LIMIT_SECONDS = 0.2


@dataclass(frozen=True, slots=True)
class FundingPriceObservation:
    coin: str
    t_ms: int
    funding: float
    perp_close: float
    trailing_mean: float
    trailing_std: float
    z_score: float


@dataclass(frozen=True, slots=True)
class ExtremeEventResult:
    """One extreme event: entry at t, exit at t + hold_hours."""

    coin: str
    entry_t_ms: int
    entry_z: float
    entry_funding: float
    entry_price: float
    exit_t_ms: int
    exit_price: float
    price_return: float           # exit/entry - 1
    funding_paid_long: float      # sum of funding over hold (long pays positive)
    long_net_return: float        # price_return - funding_paid_long
    short_net_return: float       # -price_return + funding_paid_long
    hold_hours: int


@dataclass(frozen=True, slots=True)
class ExtremeStudyResult:
    z_threshold: float
    direction: str                 # 'positive' (z > thresh) or 'negative' (z < -thresh)
    hold_hours: int
    n_events: int
    n_coins: int
    mean_price_return: float
    mean_funding_paid_long: float
    mean_long_net_return: float
    mean_short_net_return: float
    long_sharpe: float             # mean / std * sqrt(periods_per_year) — 24h => 365 periods
    short_sharpe: float
    long_t_stat: float             # mean / (std / sqrt(N))
    short_t_stat: float
    long_hit_rate: float
    short_hit_rate: float
    events: list[ExtremeEventResult]


# ---------------------------------------------------------------------------
# Candle fetcher
# ---------------------------------------------------------------------------


async def _fetch_one_coin_candles(
    client: httpx.AsyncClient,
    coin: str,
    *,
    start_ms: int,
    end_ms: int,
    interval: str = "1h",
) -> list[tuple[int, float]]:
    body: dict[str, Any] = {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms},
    }
    r = await client.post(HL_INFO_URL, json=body)
    r.raise_for_status()
    candles = r.json()
    if not isinstance(candles, list):
        return []
    out: list[tuple[int, float]] = []
    for c in candles:
        if not isinstance(c, dict) or "t" not in c or "c" not in c:
            continue
        try:
            out.append((int(c["t"]), float(c["c"])))
        except (TypeError, ValueError):
            continue
    return out


async def fetch_perp_candles_for_universe(
    coins: list[str],
    *,
    days: int = 22,
    db_path: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, list[tuple[int, float]]]:
    """Pull hourly perp candles for each coin from Hyperliquid candleSnapshot.

    Returns a dict mapping coin -> list of (t_ms, close) sorted by t_ms ascending.
    If `db_path` is provided, also persists rows to `hl_perp_candles`.
    """
    end_ms = now_ms()
    start_ms = end_ms - days * 86_400 * 1000
    out: dict[str, list[tuple[int, float]]] = {}
    async with httpx.AsyncClient(timeout=timeout) as client:
        for coin in coins:
            try:
                rows = await _fetch_one_coin_candles(
                    client, coin, start_ms=start_ms, end_ms=end_ms
                )
            except httpx.HTTPError:
                rows = []
            rows.sort(key=lambda r: r[0])
            out[coin] = rows
            await asyncio.sleep(RATE_LIMIT_SECONDS)
    if db_path is not None:
        conn = sqlite3.connect(db_path)
        try:
            persist_candles(conn, out, fetched_at=now_iso())
            conn.commit()
        finally:
            conn.close()
    return out


def persist_candles(
    conn: sqlite3.Connection,
    candles_by_coin: dict[str, list[tuple[int, float]]],
    *,
    fetched_at: str,
) -> int:
    """Write candle rows to `hl_perp_candles`. Returns number of rows inserted."""
    n = 0
    for coin, rows in candles_by_coin.items():
        for t_ms, close in rows:
            conn.execute(
                """
                INSERT OR REPLACE INTO hl_perp_candles (coin, t, close, fetched_at)
                VALUES (?, ?, ?, ?)
                """,
                (coin, int(t_ms), float(close), fetched_at),
            )
            n += 1
    return n


def load_candles(
    conn: sqlite3.Connection,
    *,
    coin: str | None = None,
) -> dict[str, list[tuple[int, float]]]:
    """Read candles back from `hl_perp_candles`, grouped by coin, sorted ascending."""
    sql = "SELECT coin, t, close FROM hl_perp_candles"
    params: list[object] = []
    if coin is not None:
        sql += " WHERE coin = ?"
        params.append(coin)
    sql += " ORDER BY coin ASC, t ASC"
    out: dict[str, list[tuple[int, float]]] = {}
    for c, t, close in conn.execute(sql, params).fetchall():
        out.setdefault(str(c), []).append((int(t), float(close)))
    return out


# ---------------------------------------------------------------------------
# Merge + z-score
# ---------------------------------------------------------------------------


def _floor_hour(t_ms: int) -> int:
    """Floor a millisecond timestamp to its containing hour boundary."""
    return (t_ms // HOUR_MS) * HOUR_MS


def merge_funding_and_prices(
    funding_rows: Sequence[FundingTick],
    candles_by_coin: dict[str, list[tuple[int, float]]],
) -> list[FundingPriceObservation]:
    """Join funding + perp_close by (coin, hour-bucket) and pre-compute the
    trailing 168h mean/std/z-score per row.

    The trailing window is rows[i - 168 : i] (exclusive of the candidate row),
    so the z-score cannot leak the candidate's own funding into its own mean.

    Rows are dropped when:
      - no candle close exists for the same (coin, hour-bucket)
      - the trailing window has fewer than 168 prior hours (early-history burn-in)
      - the trailing std is zero (constant window — z undefined)
    """
    funding_by_coin: dict[str, list[FundingTick]] = {}
    for tick in funding_rows:
        funding_by_coin.setdefault(tick.coin, []).append(tick)
    for series in funding_by_coin.values():
        series.sort(key=lambda x: x.t_ms)

    out: list[FundingPriceObservation] = []
    for coin, series in funding_by_coin.items():
        candles = candles_by_coin.get(coin, [])
        if not candles:
            continue
        close_by_hour: dict[int, float] = {_floor_hour(t): c for t, c in candles}
        fundings = [tick.funding for tick in series]
        for i, tick in enumerate(series):
            if i < TRAILING_HOURS:
                continue
            hour = _floor_hour(tick.t_ms)
            close = close_by_hour.get(hour)
            if close is None:
                continue
            window = fundings[i - TRAILING_HOURS : i]
            mean = statistics.fmean(window)
            std = statistics.pstdev(window)
            if std <= 0.0:
                continue
            z = (tick.funding - mean) / std
            out.append(
                FundingPriceObservation(
                    coin=coin,
                    t_ms=tick.t_ms,
                    funding=tick.funding,
                    perp_close=close,
                    trailing_mean=mean,
                    trailing_std=std,
                    z_score=z,
                )
            )
    out.sort(key=lambda o: (o.coin, o.t_ms))
    return out


# ---------------------------------------------------------------------------
# Event identification
# ---------------------------------------------------------------------------


def identify_extreme_events(
    obs: Sequence[FundingPriceObservation],
    *,
    z_threshold: float = 2.0,
    direction: str = "positive",
    cooldown_hours: int = 0,
) -> list[FundingPriceObservation]:
    """Filter to rows where the z-score crosses the threshold in the specified
    direction.

    `direction='positive'`  -> z >  z_threshold
    `direction='negative'`  -> z < -z_threshold

    `cooldown_hours`: per coin, after an event fires no further event is
    recorded within this many hours. cooldown=0 records every crossing,
    cooldown=hold_hours makes the events approximately independent (their
    forward holds do not overlap).
    """
    if direction not in {"positive", "negative"}:
        raise ValueError(f"direction must be 'positive' or 'negative', got {direction!r}")
    if z_threshold <= 0:
        raise ValueError("z_threshold must be positive")
    if cooldown_hours < 0:
        raise ValueError("cooldown_hours must be non-negative")

    def hit(z: float) -> bool:
        return z > z_threshold if direction == "positive" else z < -z_threshold

    last_fire_by_coin: dict[str, int] = {}
    cooldown_ms = cooldown_hours * HOUR_MS
    out: list[FundingPriceObservation] = []
    # Preserve input order within coin so cooldown filters chronologically.
    for o in sorted(obs, key=lambda o: (o.coin, o.t_ms)):
        if not hit(o.z_score):
            continue
        prev = last_fire_by_coin.get(o.coin)
        if prev is not None and o.t_ms - prev < cooldown_ms:
            continue
        out.append(o)
        last_fire_by_coin[o.coin] = o.t_ms
    return out


# ---------------------------------------------------------------------------
# Hold-to-exit P&L
# ---------------------------------------------------------------------------


def hold_to_exit(
    entries: Sequence[FundingPriceObservation],
    obs_by_coin: dict[str, list[FundingPriceObservation]],
    *,
    hold_hours: int = 24,
) -> list[ExtremeEventResult]:
    """For each entry, find the price `hold_hours` later and compute the
    return path. Drops entries whose exit row is missing (end-of-window
    truncation).

    Funding-paid-long over the hold is the sum of hourly funding rates over
    the hours strictly after entry, up to and including the exit hour. This
    is the cumulative rate a 1-unit long position pays over the hold (a
    simple-additive approximation, which is the convention used elsewhere in
    this codebase — see `hl_backtest.backtest_passive`).
    """
    if hold_hours <= 0:
        raise ValueError("hold_hours must be positive")

    out: list[ExtremeEventResult] = []
    for entry in entries:
        series = obs_by_coin.get(entry.coin, [])
        # Find the row whose hour bucket is exactly entry + hold_hours.
        target_hour = _floor_hour(entry.t_ms) + hold_hours * HOUR_MS
        exit_row = next((o for o in series if _floor_hour(o.t_ms) == target_hour), None)
        if exit_row is None:
            continue
        funding_window = [
            o.funding
            for o in series
            if _floor_hour(entry.t_ms) < _floor_hour(o.t_ms) <= target_hour
        ]
        # Require a complete funding window: hold_hours hourly payments.
        if len(funding_window) != hold_hours:
            continue
        if entry.perp_close <= 0 or exit_row.perp_close <= 0:
            continue
        price_return = exit_row.perp_close / entry.perp_close - 1.0
        funding_paid_long = sum(funding_window)
        long_net = price_return - funding_paid_long
        short_net = -price_return + funding_paid_long
        out.append(
            ExtremeEventResult(
                coin=entry.coin,
                entry_t_ms=entry.t_ms,
                entry_z=entry.z_score,
                entry_funding=entry.funding,
                entry_price=entry.perp_close,
                exit_t_ms=exit_row.t_ms,
                exit_price=exit_row.perp_close,
                price_return=price_return,
                funding_paid_long=funding_paid_long,
                long_net_return=long_net,
                short_net_return=short_net,
                hold_hours=hold_hours,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Summary statistics
# ---------------------------------------------------------------------------


def _t_stat(values: Sequence[float]) -> float:
    """One-sample t-statistic: mean / (sample_std / sqrt(N)).

    Returns 0.0 for N < 2 or zero variance. Uses sample stdev (ddof=1) — the
    standard convention for inferring a population mean from a sample.
    """
    n = len(values)
    if n < 2:
        return 0.0
    mean = statistics.fmean(values)
    std = statistics.stdev(values)
    if std <= 0.0:
        return 0.0
    return mean / (std / math.sqrt(n))


def _annualised_sharpe(values: Sequence[float], *, hold_hours: int) -> float:
    """Sharpe of per-event returns, annualised by sqrt(periods_per_year).

    periods_per_year = 8760 / hold_hours (treating each event as one
    independent per-period return). This is the convention used elsewhere in
    the codebase (hl_backtest._annualize_vol). For 24h holds the multiplier
    is sqrt(365) ~= 19.1.
    """
    n = len(values)
    if n < 2 or hold_hours <= 0:
        return 0.0
    mean = statistics.fmean(values)
    std = statistics.stdev(values)
    if std <= 0.0:
        return 0.0
    periods_per_year = HOURS_PER_YEAR / hold_hours
    return (mean / std) * math.sqrt(periods_per_year)


def _hit_rate(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return sum(1 for v in values if v > 0) / len(values)


def summarize(
    events: Sequence[ExtremeEventResult],
    *,
    z_threshold: float,
    direction: str,
    hold_hours: int,
) -> ExtremeStudyResult:
    """Aggregate per-event results into the study-level summary."""
    if not events:
        return ExtremeStudyResult(
            z_threshold=z_threshold,
            direction=direction,
            hold_hours=hold_hours,
            n_events=0,
            n_coins=0,
            mean_price_return=0.0,
            mean_funding_paid_long=0.0,
            mean_long_net_return=0.0,
            mean_short_net_return=0.0,
            long_sharpe=0.0,
            short_sharpe=0.0,
            long_t_stat=0.0,
            short_t_stat=0.0,
            long_hit_rate=0.0,
            short_hit_rate=0.0,
            events=[],
        )
    price_returns = [e.price_return for e in events]
    funding_paid = [e.funding_paid_long for e in events]
    long_returns = [e.long_net_return for e in events]
    short_returns = [e.short_net_return for e in events]
    coins = {e.coin for e in events}
    return ExtremeStudyResult(
        z_threshold=z_threshold,
        direction=direction,
        hold_hours=hold_hours,
        n_events=len(events),
        n_coins=len(coins),
        mean_price_return=statistics.fmean(price_returns),
        mean_funding_paid_long=statistics.fmean(funding_paid),
        mean_long_net_return=statistics.fmean(long_returns),
        mean_short_net_return=statistics.fmean(short_returns),
        long_sharpe=_annualised_sharpe(long_returns, hold_hours=hold_hours),
        short_sharpe=_annualised_sharpe(short_returns, hold_hours=hold_hours),
        long_t_stat=_t_stat(long_returns),
        short_t_stat=_t_stat(short_returns),
        long_hit_rate=_hit_rate(long_returns),
        short_hit_rate=_hit_rate(short_returns),
        events=list(events),
    )


def observations_by_coin(
    obs: Sequence[FundingPriceObservation],
) -> dict[str, list[FundingPriceObservation]]:
    """Group observations by coin and sort ascending by t_ms."""
    out: dict[str, list[FundingPriceObservation]] = {}
    for o in obs:
        out.setdefault(o.coin, []).append(o)
    for series in out.values():
        series.sort(key=lambda o: o.t_ms)
    return out


def run_study(
    obs: Sequence[FundingPriceObservation],
    *,
    z_threshold: float,
    direction: str,
    hold_hours: int,
    cooldown_hours: int = 0,
) -> ExtremeStudyResult:
    """End-to-end: identify -> hold-to-exit -> summarize."""
    by_coin = observations_by_coin(obs)
    entries = identify_extreme_events(
        obs, z_threshold=z_threshold, direction=direction, cooldown_hours=cooldown_hours
    )
    events = hold_to_exit(entries, by_coin, hold_hours=hold_hours)
    return summarize(
        events, z_threshold=z_threshold, direction=direction, hold_hours=hold_hours
    )
