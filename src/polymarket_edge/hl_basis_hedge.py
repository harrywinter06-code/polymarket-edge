"""Basis-hedged Hyperliquid funding-capture backtest with regime conditioning.

Plan B from `docs/plan_B_basis_hedge_regime.md`. Replaces the parametric 5 bps/leg
hedge-cost assumption in `hl_hedge.py` with a real spot/perp basis P&L sourced
from Hyperliquid candle data, and reports regime-conditional net Sharpe.

Spot-pair naming on Hyperliquid: the perp endpoint uses the coin name (e.g.
``BTC``). The spot endpoint uses ``@<index>`` where ``index`` is the spot pair
index returned by ``spotMeta``. For canonical wrapped tokens (``UBTC``,
``UETH``, ``USOL``, ``UAVAX`` ...), the base token name carries the ``U``
prefix; we match the funding-universe coin to either the bare name or the
``U``-prefixed name. Probed live before building — both name styles work
against the same ``candleSnapshot`` schema.

Coin coverage is reported transparently: not all universe coins have a spot
pair, so the hedged backtest runs on the spot-eligible subset only. The
unhedged baseline runs on all coins for the sensitivity comparison.

Bootstrap CIs use ``hl_stats.bootstrap_backtest_stats`` — do not reimplement.
At N=22 rebalances per regime the CIs are wide and the regime claim is
directional, not statistical; this is stated explicitly in the script output.
"""

from __future__ import annotations

import asyncio
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from polymarket_edge.hl_backtest import HOURS_PER_YEAR, FundingTick
from polymarket_edge.hl_stats import bootstrap_backtest_stats
from polymarket_edge.hyperliquid import HL_INFO_URL, now_ms

DEFAULT_TIMEOUT = 30.0
RATE_LIMIT_SECONDS = 0.2
HOUR_MS = 3_600_000


@dataclass(frozen=True, slots=True)
class HedgedTick:
    """One hourly observation per coin: funding + perp mark + spot mark."""

    coin: str
    t_ms: int
    funding: float
    perp_mark: float
    spot_mark: float


@dataclass(frozen=True, slots=True)
class HedgedRebalanceResult:
    """One rebalance: short K perps, long K spots, hold for ``rebalance_hours``.

    All P&L fields are per-unit-notional (per-coin), equal-weighted across the
    coins held in this rebalance.
    """

    t_ms_open: int
    t_ms_close: int
    coins_held: list[str]
    funding_received: float
    perp_pnl: float
    spot_pnl: float
    basis_pnl: float
    entry_spread_bps: float
    exit_spread_bps: float
    net_return: float


@dataclass(frozen=True, slots=True)
class HedgedBacktestResult:
    n_rebalances: int
    coins_eligible: list[str]
    coins_excluded_no_spot: list[str]
    rebalances: list[HedgedRebalanceResult]
    total_net_return: float
    annualized_net_return: float
    annualized_funding_only: float
    annualized_basis_pnl: float
    annualized_spread_cost: float
    sharpe: float
    max_drawdown: float
    hit_rate: float


@dataclass(frozen=True, slots=True)
class Regime:
    """Volatility regime classification, by trailing BTC realized vol."""

    name: str
    btc_realized_vol_trailing_7d: float


@dataclass(frozen=True, slots=True)
class RegimeConditionalResult:
    regime_name: str
    n_rebalances: int
    annualized_net_return: float
    sharpe: float
    sharpe_ci_low: float
    sharpe_ci_high: float
    ann_ret_ci_low: float
    ann_ret_ci_high: float
    max_drawdown: float


# ---------------------------------------------------------------------------
# Spot listing detection
# ---------------------------------------------------------------------------


async def _fetch_spot_meta(*, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(HL_INFO_URL, json={"type": "spotMeta"})
        r.raise_for_status()
        return r.json()


def _build_spot_index_by_base(spot_meta: dict[str, Any]) -> dict[str, str]:
    """Return ``{base_token_name: '@<index>'}`` for USDC-quoted spot pairs.

    Hyperliquid spot pairs are identified to ``candleSnapshot`` as ``@<index>``
    where index is the pair index (not the token index). Canonical pairs like
    ``PURR/USDC`` also accept the slash-form, but ``@<index>`` is universally
    safe so we standardise on it.
    """
    tokens = spot_meta.get("tokens", [])
    universe = spot_meta.get("universe", [])
    if not tokens or not universe:
        return {}
    token_name_by_index: dict[int, str] = {int(t["index"]): t["name"] for t in tokens}
    usdc_idx = next((int(t["index"]) for t in tokens if t["name"] == "USDC"), None)
    if usdc_idx is None:
        return {}
    out: dict[str, str] = {}
    for pair in universe:
        toks = pair.get("tokens")
        if not isinstance(toks, list) or len(toks) != 2:
            continue
        base_idx, quote_idx = int(toks[0]), int(toks[1])
        if quote_idx != usdc_idx:
            continue
        base_name = token_name_by_index.get(base_idx)
        if base_name is None:
            continue
        out[base_name] = f"@{int(pair['index'])}"
    return out


def _coin_to_spot_label(coin: str, spot_by_base: dict[str, str]) -> str | None:
    """Resolve a perp-coin name to its spot pair label, or None if not listed.

    Tries the bare coin name first, then the ``U``-prefixed wrapped variant
    (``UBTC``, ``UETH``, ``USOL`` ... ). Hyperliquid's canonical spot listings
    for the majors use the ``U`` prefix.
    """
    if coin in spot_by_base:
        return spot_by_base[coin]
    u_prefixed = f"U{coin}"
    if u_prefixed in spot_by_base:
        return spot_by_base[u_prefixed]
    return None


async def detect_spot_listings(
    coins: list[str], *, timeout: float = DEFAULT_TIMEOUT,
) -> tuple[list[str], list[str], dict[str, str]]:
    """Partition ``coins`` into (have_spot, no_spot) and return the spot label
    map so callers can fetch candles without re-hitting ``spotMeta``.

    Order of ``have_spot`` and ``no_spot`` preserves input order.
    """
    spot_meta = await _fetch_spot_meta(timeout=timeout)
    spot_by_base = _build_spot_index_by_base(spot_meta)
    have_spot: list[str] = []
    no_spot: list[str] = []
    label_map: dict[str, str] = {}
    for coin in coins:
        label = _coin_to_spot_label(coin, spot_by_base)
        if label is None:
            no_spot.append(coin)
        else:
            have_spot.append(coin)
            label_map[coin] = label
    return have_spot, no_spot, label_map


# ---------------------------------------------------------------------------
# Candle fetching
# ---------------------------------------------------------------------------


async def _fetch_candles(
    client: httpx.AsyncClient, *, coin_label: str, start_ms: int, end_ms: int,
    interval: str = "1h",
) -> list[dict[str, Any]]:
    body = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin_label, "interval": interval,
            "startTime": start_ms, "endTime": end_ms,
        },
    }
    r = await client.post(HL_INFO_URL, json=body)
    r.raise_for_status()
    out = r.json()
    return out if isinstance(out, list) else []


async def fetch_perp_and_spot_candles(
    coins: list[str],
    *,
    days: int = 30,
    spot_label_map: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    end_ms: int | None = None,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Pull 1h perp + spot candles for each coin. Sequential with rate-limit
    pause matching `hyperliquid.fetch_funding_history_many`.

    Coins missing from ``spot_label_map`` get an empty spot list and are
    treated as ineligible downstream.

    Returns ``{coin: {'perp': [...], 'spot': [...]}}``.
    """
    end = end_ms if end_ms is not None else now_ms()
    start = end - days * 86_400 * 1000
    labels = spot_label_map or {}
    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    async with httpx.AsyncClient(timeout=timeout) as client:
        for coin in coins:
            perp = await _fetch_candles(
                client, coin_label=coin, start_ms=start, end_ms=end,
            )
            await asyncio.sleep(RATE_LIMIT_SECONDS)
            spot_label = labels.get(coin)
            if spot_label is None:
                spot: list[dict[str, Any]] = []
            else:
                spot = await _fetch_candles(
                    client, coin_label=spot_label, start_ms=start, end_ms=end,
                )
                await asyncio.sleep(RATE_LIMIT_SECONDS)
            out[coin] = {"perp": perp, "spot": spot}
    return out


# ---------------------------------------------------------------------------
# Merging funding + candles into HedgedTicks
# ---------------------------------------------------------------------------


def _candles_to_close_map(candles: Sequence[dict[str, Any]]) -> dict[int, float]:
    out: dict[int, float] = {}
    for c in candles:
        try:
            t = int(c["t"])
            close = float(c["c"])
        except (KeyError, ValueError, TypeError):
            continue
        if close > 0:
            out[t] = close
    return out


def _floor_to_hour_ms(t_ms: int) -> int:
    """Bucket a timestamp to the top of its hour. Hyperliquid funding
    timestamps carry tens of milliseconds of jitter off the hour boundary,
    while candle timestamps land exactly on the hour. We align by truncating
    funding to the hour."""
    return (t_ms // HOUR_MS) * HOUR_MS


def merge_to_hedged_ticks(
    funding_rows: Sequence[FundingTick],
    candles: dict[str, dict[str, list[dict[str, Any]]]],
) -> list[HedgedTick]:
    """Join funding + perp_mark + spot_mark by (coin, hour).

    Funding-tick timestamps from Hyperliquid carry tens of milliseconds of
    jitter; candle timestamps are exact-on-the-hour. We floor the funding
    timestamp to its hour before joining. Drops hours where any of the three
    legs is missing. Result is sorted by (coin, t_ms). Coins absent from
    ``candles`` or with empty spot are silently dropped (no row can be
    constructed without all three legs).
    """
    by_coin: dict[str, list[FundingTick]] = {}
    for f in funding_rows:
        by_coin.setdefault(f.coin, []).append(f)
    out: list[HedgedTick] = []
    for coin, ticks in by_coin.items():
        coin_candles = candles.get(coin)
        if not coin_candles:
            continue
        perp_map = _candles_to_close_map(coin_candles.get("perp", []))
        spot_map = _candles_to_close_map(coin_candles.get("spot", []))
        if not perp_map or not spot_map:
            continue
        for ft in ticks:
            t = _floor_to_hour_ms(ft.t_ms)
            perp = perp_map.get(t)
            spot = spot_map.get(t)
            if perp is None or spot is None:
                continue
            out.append(HedgedTick(
                coin=coin, t_ms=t, funding=ft.funding,
                perp_mark=perp, spot_mark=spot,
            ))
    out.sort(key=lambda h: (h.coin, h.t_ms))
    return out


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------


def _series_by_coin(ticks: Sequence[HedgedTick]) -> dict[str, list[HedgedTick]]:
    out: dict[str, list[HedgedTick]] = {}
    for t in ticks:
        out.setdefault(t.coin, []).append(t)
    for k in out:
        out[k].sort(key=lambda x: x.t_ms)
    return out


def _common_grid(per_coin: dict[str, list[HedgedTick]]) -> list[int]:
    """Strict intersection of every coin's timestamps.

    Used only for legacy callers / tests that assume the strict invariant.
    The backtest itself uses ``_union_grid`` with per-coin gating instead.
    """
    if not per_coin:
        return []
    sets = [{t.t_ms for t in series} for series in per_coin.values()]
    common = set.intersection(*sets) if sets else set()
    return sorted(common)


def _union_grid(per_coin: dict[str, list[HedgedTick]]) -> list[int]:
    """Union of all coins' timestamps. The backtest gates per-coin per-hour
    rather than requiring all coins to be present on every hour — otherwise
    a single short-listed coin (e.g. AVAX/USDC went live mid-window)
    collapses the entire backtest grid to the late slice."""
    if not per_coin:
        return []
    all_t: set[int] = set()
    for series in per_coin.values():
        all_t.update(t.t_ms for t in series)
    return sorted(all_t)


def _drawdown(returns: Sequence[float]) -> float:
    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for r in returns:
        cum += r
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return abs(mdd)


def _annualize(per_period_return: float, hours_per_period: int) -> float:
    periods_per_year = HOURS_PER_YEAR / hours_per_period
    return per_period_return * periods_per_year


def _annualize_vol(per_period_std: float, hours_per_period: int) -> float:
    periods_per_year = HOURS_PER_YEAR / hours_per_period
    return per_period_std * (periods_per_year ** 0.5)


def backtest_hedged_top_k_trailing(
    ticks: Sequence[HedgedTick],
    *,
    top_k: int = 5,
    trailing_hours: int = 24,
    rebalance_hours: int = 8,
    entry_spread_bps_per_leg: float | None = None,
) -> HedgedBacktestResult:
    """Selection logic mirrors ``hl_backtest.backtest_top_k_trailing``.

    Each rebalance:
      - Rank coins by trailing-window mean funding.
      - Short the top K perps at ``perp_mark[t_open]``, long K spots at
        ``spot_mark[t_open]``.
      - Hold ``rebalance_hours``; receive funding hourly on the perp short.
      - Close both legs at the marks at ``t_close``.
      - If ``entry_spread_bps_per_leg`` is set, charge 2 legs at entry and
        2 legs at exit (round-trip 4 legs total). When ``None``, no extra
        spread cost beyond what the basis P&L already realises.

    Per-rebalance P&L is averaged across the coins held (equal-weight).
    """
    spread = entry_spread_bps_per_leg or 0.0
    per_leg_cost = spread / 10_000
    per_coin = _series_by_coin(ticks)
    grid = _union_grid(per_coin)
    eligible = sorted(per_coin)

    if len(grid) < trailing_hours + rebalance_hours or not per_coin:
        return HedgedBacktestResult(
            n_rebalances=0, coins_eligible=eligible, coins_excluded_no_spot=[],
            rebalances=[], total_net_return=0.0, annualized_net_return=0.0,
            annualized_funding_only=0.0, annualized_basis_pnl=0.0,
            annualized_spread_cost=0.0, sharpe=0.0, max_drawdown=0.0, hit_rate=0.0,
        )

    # Pre-build {coin: {t_ms: HedgedTick}} for O(1) lookup.
    maps: dict[str, dict[int, HedgedTick]] = {
        c: {t.t_ms: t for t in series} for c, series in per_coin.items()
    }

    rebalances: list[HedgedRebalanceResult] = []
    returns: list[float] = []
    funding_components: list[float] = []
    basis_components: list[float] = []
    spread_components: list[float] = []

    i = trailing_hours
    while i + rebalance_hours <= len(grid):
        window = grid[i - trailing_hours: i]
        trail_mean: dict[str, float] = {}
        for c, m in maps.items():
            vals = [m[t].funding for t in window if t in m]
            if len(vals) == trailing_hours:
                trail_mean[c] = statistics.fmean(vals)
        if not trail_mean:
            i += rebalance_hours
            continue
        top = sorted(trail_mean.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        held = [c for c, _ in top]

        t_open = grid[i]
        t_close_idx = i + rebalance_hours - 1
        t_close = grid[t_close_idx]
        future_hours = grid[i: i + rebalance_hours]

        coin_funding: list[float] = []
        coin_perp_pnl: list[float] = []
        coin_spot_pnl: list[float] = []
        coins_realized: list[str] = []
        for c in held:
            m = maps[c]
            open_tick = m.get(t_open)
            close_tick = m.get(t_close)
            if open_tick is None or close_tick is None:
                continue
            funding_vals = [m[t].funding for t in future_hours if t in m]
            if len(funding_vals) != len(future_hours):
                continue
            # Short perp: profit when perp falls. Per-unit-notional return
            # is normalized by entry price.
            perp_pnl = (open_tick.perp_mark - close_tick.perp_mark) / open_tick.perp_mark
            # Long spot: profit when spot rises.
            spot_pnl = (close_tick.spot_mark - open_tick.spot_mark) / open_tick.spot_mark
            coin_funding.append(sum(funding_vals))
            coin_perp_pnl.append(perp_pnl)
            coin_spot_pnl.append(spot_pnl)
            coins_realized.append(c)

        if not coins_realized:
            i += rebalance_hours
            continue

        avg_funding = statistics.fmean(coin_funding)
        avg_perp = statistics.fmean(coin_perp_pnl)
        avg_spot = statistics.fmean(coin_spot_pnl)
        basis = avg_perp + avg_spot
        entry_spread_cost = 2 * per_leg_cost
        exit_spread_cost = 2 * per_leg_cost
        total_spread_cost = entry_spread_cost + exit_spread_cost
        net = avg_funding + basis - total_spread_cost

        rebalances.append(HedgedRebalanceResult(
            t_ms_open=t_open, t_ms_close=t_close,
            coins_held=coins_realized,
            funding_received=avg_funding,
            perp_pnl=avg_perp, spot_pnl=avg_spot,
            basis_pnl=basis,
            entry_spread_bps=entry_spread_cost * 10_000,
            exit_spread_bps=exit_spread_cost * 10_000,
            net_return=net,
        ))
        returns.append(net)
        funding_components.append(avg_funding)
        basis_components.append(basis)
        spread_components.append(total_spread_cost)
        i += rebalance_hours

    if not returns:
        return HedgedBacktestResult(
            n_rebalances=0, coins_eligible=eligible, coins_excluded_no_spot=[],
            rebalances=[], total_net_return=0.0, annualized_net_return=0.0,
            annualized_funding_only=0.0, annualized_basis_pnl=0.0,
            annualized_spread_cost=0.0, sharpe=0.0, max_drawdown=0.0, hit_rate=0.0,
        )

    total = sum(returns)
    mean = statistics.fmean(returns)
    std = statistics.pstdev(returns) if len(returns) >= 2 else 0.0
    ann_ret = _annualize(mean, rebalance_hours)
    ann_vol = _annualize_vol(std, rebalance_hours)
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
    hits = sum(1 for r in returns if r > 0) / len(returns)
    return HedgedBacktestResult(
        n_rebalances=len(returns),
        coins_eligible=eligible,
        coins_excluded_no_spot=[],
        rebalances=rebalances,
        total_net_return=total,
        annualized_net_return=ann_ret,
        annualized_funding_only=_annualize(
            statistics.fmean(funding_components), rebalance_hours,
        ),
        annualized_basis_pnl=_annualize(
            statistics.fmean(basis_components), rebalance_hours,
        ),
        annualized_spread_cost=_annualize(
            statistics.fmean(spread_components), rebalance_hours,
        ),
        sharpe=sharpe,
        max_drawdown=_drawdown(returns),
        hit_rate=hits,
    )


# ---------------------------------------------------------------------------
# Regime classification
# ---------------------------------------------------------------------------


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = k - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def classify_regimes(
    btc_perp_candles: Sequence[dict[str, Any]],
    *,
    vol_window_hours: int = 168,
) -> dict[int, Regime]:
    """For each hour t, compute trailing realized log-return vol and bucket
    into terciles (low/med/high).

    Returns ``{t_ms: Regime}``. Hours without a full trailing window are
    excluded. Tercile cutoffs are the 33.33rd and 66.67th percentiles of the
    in-sample trailing-vol distribution (no look-ahead within the regime
    label of a single bucket, but the bucket boundaries themselves are
    derived from the full dataset — standard regime-classification practice
    for retrospective analysis).
    """
    rows = sorted(
        ((int(c["t"]), float(c["c"])) for c in btc_perp_candles
         if "t" in c and "c" in c and float(c["c"]) > 0),
        key=lambda x: x[0],
    )
    if len(rows) < vol_window_hours + 2:
        return {}
    log_returns: list[tuple[int, float]] = []
    for i in range(1, len(rows)):
        _, prev_c = rows[i - 1]
        t, c = rows[i]
        log_returns.append((t, math.log(c / prev_c)))
    trailing_vols: list[tuple[int, float]] = []
    for i in range(vol_window_hours, len(log_returns) + 1):
        window = log_returns[i - vol_window_hours: i]
        rs = [r for _, r in window]
        mean = sum(rs) / len(rs)
        var = sum((r - mean) ** 2 for r in rs) / len(rs)
        vol = math.sqrt(var)
        end_t = log_returns[i - 1][0]
        trailing_vols.append((end_t, vol))
    if not trailing_vols:
        return {}
    sorted_vols = sorted(v for _, v in trailing_vols)
    p33 = _percentile(sorted_vols, 100.0 / 3.0)
    p67 = _percentile(sorted_vols, 200.0 / 3.0)
    out: dict[int, Regime] = {}
    for t, v in trailing_vols:
        if v <= p33:
            name = "low"
        elif v <= p67:
            name = "med"
        else:
            name = "high"
        out[t] = Regime(name=name, btc_realized_vol_trailing_7d=v)
    return out


def regime_conditional_results(
    result: HedgedBacktestResult,
    regimes: dict[int, Regime],
    *,
    rebalance_hours: int,
    n_bootstrap: int = 2000,
) -> list[RegimeConditionalResult]:
    """Bucket each rebalance by the regime at its open time; compute per-regime
    stats with bootstrap CI on Sharpe and annualized return.

    Rebalances whose open time has no regime label (insufficient trailing
    window) are silently dropped.
    """
    buckets: dict[str, list[float]] = {"low": [], "med": [], "high": []}
    for rb in result.rebalances:
        # Nearest-prior regime label: regimes are keyed on the log-return
        # timestamp, which is the *closing* edge of the trailing window.
        # The most informative label for an open at t is the regime ending
        # at or just before t.
        regime = regimes.get(rb.t_ms_open)
        if regime is None:
            # Try the immediately prior hour as a fallback.
            regime = regimes.get(rb.t_ms_open - HOUR_MS)
        if regime is None:
            continue
        buckets[regime.name].append(rb.net_return)

    out: list[RegimeConditionalResult] = []
    for name in ("low", "med", "high"):
        returns = buckets[name]
        if not returns:
            out.append(RegimeConditionalResult(
                regime_name=name, n_rebalances=0,
                annualized_net_return=0.0, sharpe=0.0,
                sharpe_ci_low=0.0, sharpe_ci_high=0.0,
                ann_ret_ci_low=0.0, ann_ret_ci_high=0.0,
                max_drawdown=0.0,
            ))
            continue
        mean = statistics.fmean(returns)
        std = statistics.pstdev(returns) if len(returns) >= 2 else 0.0
        ann_ret = _annualize(mean, rebalance_hours)
        ann_vol = _annualize_vol(std, rebalance_hours)
        sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
        stats = bootstrap_backtest_stats(
            returns, hours_per_period=rebalance_hours,
            n_resamples=n_bootstrap, seed=42,
        )
        out.append(RegimeConditionalResult(
            regime_name=name,
            n_rebalances=len(returns),
            annualized_net_return=ann_ret,
            sharpe=sharpe,
            sharpe_ci_low=stats.sharpe.ci_low,
            sharpe_ci_high=stats.sharpe.ci_high,
            ann_ret_ci_low=stats.annualized_return.ci_low,
            ann_ret_ci_high=stats.annualized_return.ci_high,
            max_drawdown=_drawdown(returns),
        ))
    return out
