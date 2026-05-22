"""Market-maker P&L simulator for Polymarket negRisk events.

Pulls historical trades for each constituent market of an event and estimates
the per-day net P&L a maker would earn capturing a fraction of that flow, net
of an adverse-selection charge expressed as a fraction of the observed half-
spread.

Reports under three adverse-selection scenarios (naive / moderate / informed)
and exposes a breakeven half-spread fraction — the AS fraction at which net
P&L crosses zero.

Endpoint reality (verified by probe on 2026-05-22):

  - ``clob.polymarket.com/trades`` is auth-gated (401 without API key).
  - ``data-api.polymarket.com/trades`` is public and filters by
    ``market=<conditionId>`` (NOT by token id; the ``asset`` and ``token`` query
    params are silently ignored and return global flow). The spec's best-guess
    ``market=<token_id>`` was off-by-one — fixed here by passing the
    conditionId.
  - Max pagination depth is offset=3000 (server returns 400 "max historical
    activity offset of 3000 exceeded" beyond that). No ``before``/``after``/
    ``endTime`` cursor is honored. The lookback window is therefore the time
    spanned by the most-recent 3000 trades, capped further by
    ``lookback_days``.

Adverse selection is parameterized as a fraction of the half-spread observed
from realized price movement over the next K minutes (default 5) following
each trade. Without orderbook snapshots over time this is the cleanest
proxy: in a competitive book the inside spread is implied by how far price
moves between trades, and the AS cost to a maker is some fraction of that.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

DATA_API_BASE = "https://data-api.polymarket.com"
DEFAULT_TIMEOUT = 30.0
RATE_LIMIT_SECONDS = 0.25
MAX_OFFSET = 3000              # server-enforced; see module docstring
PAGE_SIZE = 500


@dataclass(frozen=True, slots=True)
class Trade:
    """One historical trade on a CLOB market."""

    token_id: str
    timestamp_s: int
    price: float
    size_shares: float
    taker_side: str  # 'BUY' or 'SELL'


@dataclass(frozen=True, slots=True)
class AdverseSelectionScenario:
    """How much adverse-selection cost to charge per maker fill, expressed
    as a fraction of the half-spread."""

    name: str
    realized_half_spread_fraction: float
    description: str


@dataclass(frozen=True, slots=True)
class MarketMMResult:
    token_id: str
    market_question: str
    n_trades_observed: int
    estimated_maker_fills: int
    gross_rebate_usd: float
    adverse_selection_cost_usd: float
    net_pnl_usd: float
    per_day_net_usd: float
    days_observed: float


@dataclass(frozen=True, slots=True)
class EventMMResult:
    event_slug: str
    event_title: str
    scenario: AdverseSelectionScenario
    n_markets_simulated: int
    total_gross_rebate_usd: float
    total_adverse_selection_usd: float
    total_net_pnl_usd: float
    per_day_net_usd: float
    days_observed: float
    projected_pnl_to_resolution_usd: float
    per_market: list[MarketMMResult] = field(default_factory=list)


# Canonical scenarios -------------------------------------------------------

NAIVE_SCENARIO = AdverseSelectionScenario(
    name="naive",
    realized_half_spread_fraction=0.0,
    description="no adverse selection — upper bound on MM P&L",
)
MODERATE_SCENARIO = AdverseSelectionScenario(
    name="moderate",
    realized_half_spread_fraction=0.5,
    description="maker pays half the spread on average (textbook MM literature)",
)
INFORMED_SCENARIO = AdverseSelectionScenario(
    name="informed",
    realized_half_spread_fraction=1.0,
    description="maker pays full spread on every fill — pessimistic",
)
SCENARIOS: tuple[AdverseSelectionScenario, ...] = (
    NAIVE_SCENARIO,
    MODERATE_SCENARIO,
    INFORMED_SCENARIO,
)


# Trade fetch ---------------------------------------------------------------


async def fetch_trades_for_token(
    client: httpx.AsyncClient,
    token_id: str,
    *,
    lookback_days: int = 30,
    condition_id: str | None = None,
    rate_limit_seconds: float = RATE_LIMIT_SECONDS,
) -> list[Trade]:
    """Fetch historical trades for a single token, paginated.

    The data-api ``/trades`` endpoint filters by ``market=<conditionId>``, not by
    token id (probe-verified 2026-05-22 — the documented ``asset``/``token``
    params are silently ignored). Caller must pass ``condition_id`` to get
    market-scoped results; if omitted the function returns an empty list rather
    than silently returning unrelated global trades.

    Returned trades are filtered to ``asset == token_id`` to avoid
    double-counting the complementary NO-side trades that the conditionId-
    scoped endpoint returns alongside YES trades.
    """
    if not condition_id:
        return []

    cutoff_s = _now_s() - lookback_days * 86400
    trades: list[Trade] = []
    offset = 0
    while offset <= MAX_OFFSET:
        params = {"market": condition_id, "limit": PAGE_SIZE, "offset": offset}
        r = await client.get(f"{DATA_API_BASE}/trades", params=params)
        if r.status_code != 200:
            break
        payload = r.json()
        if not isinstance(payload, list) or not payload:
            break
        page_stopped_early = False
        for d in payload:
            ts = int(d.get("timestamp") or 0)
            if ts < cutoff_s:
                page_stopped_early = True
                continue
            if str(d.get("asset")) != str(token_id):
                continue
            try:
                trades.append(
                    Trade(
                        token_id=str(token_id),
                        timestamp_s=ts,
                        price=float(d.get("price")),
                        size_shares=float(d.get("size")),
                        taker_side=str(d.get("side") or "").upper(),
                    )
                )
            except (TypeError, ValueError):
                continue
        if len(payload) < PAGE_SIZE or page_stopped_early:
            break
        offset += PAGE_SIZE
        if offset > MAX_OFFSET:
            break
        await asyncio.sleep(rate_limit_seconds)
    return trades


async def fetch_trades_for_event(
    markets: list[dict[str, Any]],
    *,
    lookback_days: int = 30,
    timeout: float = DEFAULT_TIMEOUT,
    rate_limit_seconds: float = RATE_LIMIT_SECONDS,
) -> dict[str, list[Trade]]:
    """Fetch trades for every market in an event. Returns {yes_token: trades}."""
    out: dict[str, list[Trade]] = {}
    async with httpx.AsyncClient(timeout=timeout) as client:
        for m in markets:
            yes_token = _yes_token(m)
            cond = m.get("conditionId")
            if not yes_token or not cond:
                continue
            trades = await fetch_trades_for_token(
                client,
                yes_token,
                lookback_days=lookback_days,
                condition_id=str(cond),
                rate_limit_seconds=rate_limit_seconds,
            )
            out[yes_token] = trades
            await asyncio.sleep(rate_limit_seconds)
    return out


# Simulator -----------------------------------------------------------------


def estimate_half_spread(trades: list[Trade], *, window_minutes: int = 5) -> float:
    """Estimate the typical half-spread as the mean absolute price change
    over a ``window_minutes`` window following each trade, divided by two.

    Without orderbook snapshots this is the cleanest proxy: in a competitive
    book the spread is implied by realized price drift between adjacent trades.
    Returned in price-points (decimal in [0, 1]), so a half-spread of 0.005
    means $0.005 per share.

    Uses mean (not median) because Polymarket prices are quantized at $0.01
    and many 5-min windows see zero realized change at the median; the mean
    of absolute changes captures the actual reverting flow that informs the
    spread.
    """
    if len(trades) < 2:
        return 0.0
    window_s = window_minutes * 60
    sorted_t = sorted(trades, key=lambda t: t.timestamp_s)
    deltas: list[float] = []
    j = 0
    for i, ti in enumerate(sorted_t):
        target = ti.timestamp_s + window_s
        j = max(j, i + 1)
        while j < len(sorted_t) and sorted_t[j].timestamp_s < target:
            j += 1
        if j >= len(sorted_t):
            break
        deltas.append(abs(sorted_t[j].price - ti.price))
    if not deltas:
        return 0.0
    return sum(deltas) / len(deltas) / 2.0


def simulate_market_maker(
    trades: list[Trade],
    *,
    scenario: AdverseSelectionScenario,
    market_question: str = "",
    token_id: str = "",
    maker_rebate_bps_of_notional: float = 18.75,
    sole_maker_capture_fraction: float = 0.5,
    half_spread_window_minutes: int = 5,
) -> MarketMMResult:
    """Run the sim for a single market.

    Logic:
      - Maker captures ``sole_maker_capture_fraction`` of observed trades.
      - Gross rebate per captured trade = trade_notional * rebate_bps / 10000.
      - AS cost per captured trade = trade_notional * (half_spread / price) *
        scenario.realized_half_spread_fraction. The (half_spread / price) term
        is unitless — the spread cost is the fraction of notional that the
        maker gives up to adverse selection. Charging it this way keeps both
        the rebate and the AS cost in the same units (USD / notional).
      - Per-day net P&L = total net / observed days; observed days from
        timestamp span of input trades.
    """
    if not trades:
        return MarketMMResult(
            token_id=token_id,
            market_question=market_question,
            n_trades_observed=0,
            estimated_maker_fills=0,
            gross_rebate_usd=0.0,
            adverse_selection_cost_usd=0.0,
            net_pnl_usd=0.0,
            per_day_net_usd=0.0,
            days_observed=0.0,
        )

    half_spread = estimate_half_spread(trades, window_minutes=half_spread_window_minutes)
    n_total = len(trades)
    captured = max(0, round(n_total * sole_maker_capture_fraction))

    if captured == 0:
        days_observed = _days_spanned(trades)
        return MarketMMResult(
            token_id=token_id,
            market_question=market_question,
            n_trades_observed=n_total,
            estimated_maker_fills=0,
            gross_rebate_usd=0.0,
            adverse_selection_cost_usd=0.0,
            net_pnl_usd=0.0,
            per_day_net_usd=0.0,
            days_observed=days_observed,
        )

    # Use mean notional, weighted equally across the captured fraction so the
    # sim doesn't bias toward whatever order trades were sampled in.
    notionals = [t.price * t.size_shares for t in trades]
    total_notional_captured = sum(notionals) * sole_maker_capture_fraction
    mean_price = sum(t.price for t in trades) / n_total
    avg_half_spread_fraction = (half_spread / mean_price) if mean_price > 0 else 0.0

    gross_rebate = total_notional_captured * (maker_rebate_bps_of_notional / 10_000.0)
    as_cost = (
        total_notional_captured
        * avg_half_spread_fraction
        * scenario.realized_half_spread_fraction
    )
    net = gross_rebate - as_cost
    days_observed = _days_spanned(trades)
    per_day = net / days_observed if days_observed > 0 else 0.0

    return MarketMMResult(
        token_id=token_id,
        market_question=market_question,
        n_trades_observed=n_total,
        estimated_maker_fills=captured,
        gross_rebate_usd=gross_rebate,
        adverse_selection_cost_usd=as_cost,
        net_pnl_usd=net,
        per_day_net_usd=per_day,
        days_observed=days_observed,
    )


def simulate_basket(
    trades_by_token: dict[str, list[Trade]],
    market_questions: dict[str, str],
    *,
    scenario: AdverseSelectionScenario,
    event_slug: str,
    event_title: str,
    days_to_resolution: float,
    maker_rebate_bps_of_notional: float = 18.75,
    sole_maker_capture_fraction: float = 0.5,
    half_spread_window_minutes: int = 5,
) -> EventMMResult:
    """Aggregate per-market sim into an event-level result with projection."""
    per_market: list[MarketMMResult] = []
    for token_id, trades in trades_by_token.items():
        per_market.append(
            simulate_market_maker(
                trades,
                scenario=scenario,
                market_question=market_questions.get(token_id, ""),
                token_id=token_id,
                maker_rebate_bps_of_notional=maker_rebate_bps_of_notional,
                sole_maker_capture_fraction=sole_maker_capture_fraction,
                half_spread_window_minutes=half_spread_window_minutes,
            )
        )

    total_rebate = sum(r.gross_rebate_usd for r in per_market)
    total_as = sum(r.adverse_selection_cost_usd for r in per_market)
    total_net = total_rebate - total_as

    # Use the longest observed window across markets as the basket's observed
    # span — markets without trades shouldn't shrink the denominator.
    observed_days = max((r.days_observed for r in per_market), default=0.0)
    per_day = total_net / observed_days if observed_days > 0 else 0.0
    projected = per_day * days_to_resolution

    return EventMMResult(
        event_slug=event_slug,
        event_title=event_title,
        scenario=scenario,
        n_markets_simulated=sum(1 for r in per_market if r.n_trades_observed > 0),
        total_gross_rebate_usd=total_rebate,
        total_adverse_selection_usd=total_as,
        total_net_pnl_usd=total_net,
        per_day_net_usd=per_day,
        days_observed=observed_days,
        projected_pnl_to_resolution_usd=projected,
        per_market=per_market,
    )


def breakeven_half_spread_fraction(
    trades_by_token: dict[str, list[Trade]],
    *,
    maker_rebate_bps_of_notional: float = 18.75,
    sole_maker_capture_fraction: float = 0.5,
    half_spread_window_minutes: int = 5,
) -> float:
    """Return the AS half-spread fraction at which net basket P&L crosses zero.

    Net = gross_rebate - as_cost = N * (rebate_rate - frac * spread_frac).
    Solve for frac: frac_breakeven = rebate_rate / spread_frac, weighted by
    captured notional across markets.
    """
    total_notional_captured = 0.0
    total_spread_cost_per_unit_frac = 0.0
    for trades in trades_by_token.values():
        if not trades:
            continue
        half_spread = estimate_half_spread(trades, window_minutes=half_spread_window_minutes)
        mean_price = sum(t.price for t in trades) / len(trades)
        if mean_price <= 0:
            continue
        spread_fraction = half_spread / mean_price
        notional = sum(t.price * t.size_shares for t in trades) * sole_maker_capture_fraction
        total_notional_captured += notional
        total_spread_cost_per_unit_frac += notional * spread_fraction

    if total_spread_cost_per_unit_frac <= 0:
        return float("inf")
    rebate_per_notional = maker_rebate_bps_of_notional / 10_000.0
    total_rebate = total_notional_captured * rebate_per_notional
    return total_rebate / total_spread_cost_per_unit_frac


# Helpers -------------------------------------------------------------------


def _yes_token(market: dict[str, Any]) -> str | None:
    raw = market.get("clobTokenIds")
    if not raw:
        return None
    try:
        tokens = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return None
    if not tokens:
        return None
    return str(tokens[0])


def _days_spanned(trades: list[Trade]) -> float:
    if len(trades) < 2:
        return 0.0
    ts = [t.timestamp_s for t in trades]
    return (max(ts) - min(ts)) / 86400.0


def _now_s() -> int:
    import time
    return int(time.time())
