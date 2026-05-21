"""Size a single real basket trade on a flagged Polymarket negRisk event.

Given an event slug and a target USD notional, this script:
  1. Pulls every active event from gamma and locates the slug.
  2. Detects the category (Sports / Politics / Crypto / Geopolitical / Culture)
     from the event tags, with --category override.
  3. Auto-picks the side from the top-of-book gap sign unless --side is given.
  4. Walks the full /book for every constituent market and computes the
     depth-aware basket fill at $total_usd / n_markets per market.
  5. Computes expected fees (per-category, taker) and rebates (maker),
     expected fill price + share count per market, net P&L at settlement,
     and a kill-the-trade-if line at a configurable bps threshold.

Output is plain text suitable for pasting into TRADE_LOG.md.

Run:
    $env:PYTHONPATH = "src"
    python scripts/size_basket_trade.py --slug 2026-fifa-world-cup-winner-595 \
        --total-usd 20 --maker

Pre-mortem: the most likely failure is a stale or mistyped slug. We handle it
by listing the closest matches and pointing at `polymarket-edge stats`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from polymarket_edge import book_depth, fetch  # noqa: E402
from polymarket_edge.detector import score_event  # noqa: E402

# Per-category PEAK taker fee (probability-curved on Polymarket; we use peak as
# a conservative upper-bound for the basket where many markets trade near the
# tail probabilities but at least one will be near 50%). See REDTEAM.md §1a.
TAKER_FEE_BPS: dict[str, float] = {
    "sports": 75.0,         # 0.75%
    "politics": 100.0,      # 1.00%
    "economics": 150.0,     # 1.50%
    "crypto": 180.0,        # 1.80%
    "culture": 125.0,       # ~1.25% (Culture / Mentions)
    "geopolitical": 0.0,    # 0.00%
}
MAKER_FEE_BPS = 0.0
MAKER_REBATE_BPS = 20.0  # conservative end of the 20-25% rebate band -> 20bps
DEFAULT_KILL_THRESHOLD_BPS = 30.0


@dataclass(frozen=True, slots=True)
class CategoryDetection:
    category: str
    source: str   # "tag" | "override" | "default-politics"


@dataclass(frozen=True, slots=True)
class PerMarketEstimate:
    question: str
    side: str
    top_of_book_price: float
    depth_aware_avg_fill: float
    notional_usd: float
    shares: float
    book_exhausted: bool
    throttle_at_usd: float


def detect_category(event: dict[str, Any], override: str | None) -> CategoryDetection:
    """Map an event's tags to one of TAKER_FEE_BPS keys. Override wins if given."""
    if override:
        norm = override.strip().lower()
        if norm not in TAKER_FEE_BPS:
            raise SystemExit(
                f"--category {override!r} unknown. valid: {sorted(TAKER_FEE_BPS)}"
            )
        return CategoryDetection(category=norm, source="override")
    tag_slugs = {(t.get("slug") or "").lower() for t in event.get("tags") or []}
    tag_labels = {(t.get("label") or "").lower() for t in event.get("tags") or []}
    bag = tag_slugs | tag_labels
    # Order matters: most-specific first. "geopolitical" before "politics".
    if {"geopolitical", "geopolitics", "war", "iran", "israel", "russia-ukraine"} & bag:
        return CategoryDetection("geopolitical", "tag")
    if {"sports", "soccer", "nfl", "nba", "mlb", "tennis", "f1", "formula-1"} & bag:
        return CategoryDetection("sports", "tag")
    if {"crypto", "bitcoin", "ethereum", "btc", "eth"} & bag:
        return CategoryDetection("crypto", "tag")
    if {"economics", "fed", "rates", "inflation", "cpi"} & bag:
        return CategoryDetection("economics", "tag")
    if {"politics", "elections", "us-election"} & bag:
        return CategoryDetection("politics", "tag")
    if {"culture", "mentions", "entertainment", "celebrities", "awards"} & bag:
        return CategoryDetection("culture", "tag")
    # Conservative default: assume Politics fee (1.0%) so we never under-charge.
    return CategoryDetection("politics", "default-politics")


def fee_bps_for(category: str, *, maker: bool) -> float:
    """Round-trip-equivalent fee in bps for a single basket leg."""
    if maker:
        # Maker pays 0% and earns a rebate. Net is negative (a credit).
        return MAKER_FEE_BPS - MAKER_REBATE_BPS
    return TAKER_FEE_BPS[category]


async def load_event(slug: str) -> dict[str, Any]:
    events = await fetch.fetch_all_active_events(max_events=500)
    match = next((e for e in events if (e.get("slug") or "") == slug), None)
    if match is not None:
        return match
    # Graceful failure with suggestions.
    needle = slug.lower()
    close = [
        e.get("slug")
        for e in events
        if e.get("slug") and any(part in (e["slug"]).lower() for part in needle.split("-") if part)
    ][:8]
    msg = [f"event slug not found: {slug!r}"]
    if close:
        msg.append("did you mean one of:")
        msg.extend(f"  - {s}" for s in close)
    msg.append("list all flagged events: `uv run polymarket-edge stats`")
    raise SystemExit("\n".join(msg))


def autodetect_side(signal_bid_gap: float, signal_ask_gap: float) -> str:
    if signal_bid_gap >= signal_ask_gap and signal_bid_gap > 0:
        return "sell_yes"
    if signal_ask_gap > 0:
        return "buy_yes"
    # No flagged direction — caller decides. Default to sell_yes (script will
    # report the negative gap honestly so the kill-line trips).
    return "sell_yes"


def _active_markets(event: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        m for m in event.get("markets", [])
        if m.get("active") and not m.get("closed") and m.get("acceptingOrders")
    ]


def _per_market_breakdown(
    active: list[dict[str, Any]],
    books: dict[str, book_depth.MarketBook],
    side: str,
    notional_per_market: float,
) -> list[PerMarketEstimate]:
    out: list[PerMarketEstimate] = []
    for m in active:
        raw = m.get("clobTokenIds")
        if not raw:
            continue
        try:
            tokens = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        if not tokens:
            continue
        yes_id = str(tokens[0])
        book = books.get(yes_id)
        if book is None:
            continue
        if side == "sell_yes":
            top = float(m.get("bestBid") or 0.0)
            walk = book_depth.walk_side(book.bids, notional_per_market)
        else:
            top = float(m.get("bestAsk") or 0.0)
            walk = book_depth.walk_side(book.asks, notional_per_market)
        out.append(
            PerMarketEstimate(
                question=(m.get("question") or yes_id)[:60],
                side=side,
                top_of_book_price=top,
                depth_aware_avg_fill=walk.avg_fill_price,
                notional_usd=walk.consumed_notional_usd,
                shares=walk.consumed_shares,
                book_exhausted=walk.book_exhausted,
                throttle_at_usd=walk.consumed_notional_usd if walk.book_exhausted else float("inf"),
            )
        )
    return out


def _settlement_pnl_usd(
    rows: list[PerMarketEstimate],
    side: str,
    fee_bps: float,
) -> float:
    """Probability-weighted expected P&L at settlement.

    For a negRisk basket: exactly one constituent settles at $1, all others at $0.
    The "fair" probability of each YES outcome is approximately its current price.

    Sell-side: receive `avg_fill` per share now, pay $1 only on the winning market.
        per-share P&L on the basket =  sum(avg_fill_i * shares_i) - 1 * shares_winner
        Expected over winner ~ price_i:
            E[pnl] = sum(avg_fill_i * shares_i) - sum(price_i * shares_i_topbook_proxy)
        Simplified: we report sum(avg_fill_i * shares_i) - max(shares_i across markets)
        scaled by 1.0 since exactly one share-bundle pays $1 per share. But shares
        differ per market (different fill prices), so use the more careful form:
        E[payout] = sum_i (P[market i wins] * shares_i * 1.0)
                  = sum_i (price_i * shares_i)  using top-of-book price as P proxy
        E[cash_in] = sum_i (avg_fill_i * shares_i)  [we sold YES, received this]
        Net (gross of fees) = E[cash_in] - E[payout]

    Buy-side mirror: we pay `avg_fill` now, receive $1 per share on the winner.
        E[payout] = sum_i (price_i * shares_i)
        E[cash_out] = sum_i (avg_fill_i * shares_i)
        Net (gross of fees) = E[payout] - E[cash_out]

    Fees are charged on traded notional, both legs (entry now, settlement exit
    implicit at $1/$0). We approximate as a single entry-side fee on the cash
    leg; the settlement leg has no taker fee on Polymarket (resolution payout).
    """
    cash_leg = 0.0
    expected_payout = 0.0
    fee_paid = 0.0
    for r in rows:
        cash_leg += r.depth_aware_avg_fill * r.shares
        # Use top-of-book price as the implied probability proxy.
        expected_payout += r.top_of_book_price * r.shares
        fee_paid += (fee_bps / 10_000.0) * r.depth_aware_avg_fill * r.shares
    gross = cash_leg - expected_payout if side == "sell_yes" else expected_payout - cash_leg
    return gross - fee_paid


def _fmt_usd(x: float) -> str:
    return f"${x:+,.4f}"


def _fmt_bps(frac: float) -> str:
    return f"{frac * 10_000:+.2f} bps"


def build_report(
    *,
    event: dict[str, Any],
    category: CategoryDetection,
    side: str,
    total_usd: float,
    maker: bool,
    rows: list[PerMarketEstimate],
    basket_result: book_depth.EventDepthResult,
    sum_top_other_side: float,
    kill_bps: float,
) -> str:
    n = len(rows)
    per_mkt = total_usd / n if n else 0.0
    fee_bps_taker = TAKER_FEE_BPS[category.category]
    fee_bps_maker = MAKER_FEE_BPS - MAKER_REBATE_BPS  # negative => credit
    fee_bps_used = fee_bps_for(category.category, maker=maker)

    gross_gap_bps = basket_result.gap_depth_aware * 10_000  # signed bps
    net_gap_bps_taker = gross_gap_bps - fee_bps_taker
    net_gap_bps_maker = gross_gap_bps - fee_bps_maker  # subtract negative => add rebate
    net_gap_bps_used = gross_gap_bps - fee_bps_used

    pnl_used = _settlement_pnl_usd(rows, side, fee_bps_used)
    pnl_taker = _settlement_pnl_usd(rows, side, fee_bps_taker)
    pnl_maker = _settlement_pnl_usd(rows, side, fee_bps_maker)

    kill_trigger = net_gap_bps_used < kill_bps
    go_label = "NO-GO (below kill threshold)" if kill_trigger else "GO"

    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("polymarket-edge :: basket sizing report")
    lines.append("=" * 78)
    lines.append(f"event:        {event.get('title')}")
    lines.append(f"slug:         {event.get('slug')}")
    lines.append(
        f"structure:    negRisk={event.get('negRisk')} "
        f"negRiskAugmented={event.get('negRiskAugmented')}"
    )
    lines.append(f"n_active:     {n} markets")
    lines.append(
        f"category:     {category.category} "
        f"(source={category.source}, taker_fee={fee_bps_taker:.0f} bps, "
        f"maker={MAKER_FEE_BPS:.0f} bps + {MAKER_REBATE_BPS:.0f} bps rebate)"
    )
    lines.append(f"side:         {side}")
    mode_text = "MAKER (limit at top of book)" if maker else "TAKER (cross spread)"
    lines.append(f"execution:    {mode_text}")
    lines.append(f"total notional: ${total_usd:.2f}  -> ${per_mkt:.4f} per market")
    lines.append("")
    lines.append("top-of-book sanity check")
    lines.append("-" * 78)
    if side == "sell_yes":
        lines.append(
            f"  sum(bestBid YES) = {basket_result.sum_top_of_book:.4f}  "
            f"(gap = {basket_result.gap_top_of_book * 10_000:+.2f} bps)"
        )
        lines.append(f"  sum(bestAsk YES) = {sum_top_other_side:.4f}")
    else:
        lines.append(
            f"  sum(bestAsk YES) = {basket_result.sum_top_of_book:.4f}  "
            f"(gap = {basket_result.gap_top_of_book * 10_000:+.2f} bps)"
        )
        lines.append(f"  sum(bestBid YES) = {sum_top_other_side:.4f}")

    lines.append("")
    lines.append("depth-aware basket fill")
    lines.append("-" * 78)
    lines.append(
        f"  sum(avg_fill) = {basket_result.sum_avg_fill:.6f}  "
        f"(gap = {_fmt_bps(basket_result.gap_depth_aware)} gross)"
    )
    lines.append(
        f"  throttle: {basket_result.basket_throttle_market!r} "
        f"(consumed ${basket_result.basket_throttle_notional:.2f})"
    )
    lines.append("")
    lines.append("maker vs taker")
    lines.append("-" * 78)
    lines.append(
        f"  taker net gap = {gross_gap_bps:+.2f} - {fee_bps_taker:.2f} = "
        f"{net_gap_bps_taker:+.2f} bps   (E[P&L] = {_fmt_usd(pnl_taker)})"
    )
    lines.append(
        f"  maker net gap = {gross_gap_bps:+.2f} - ({fee_bps_maker:+.2f}) = "
        f"{net_gap_bps_maker:+.2f} bps   (E[P&L] = {_fmt_usd(pnl_maker)})"
    )
    lines.append(
        f"  >> using {'MAKER' if maker else 'TAKER'}: "
        f"net = {net_gap_bps_used:+.2f} bps, E[P&L] = {_fmt_usd(pnl_used)}"
    )
    lines.append("")
    lines.append("per-market breakdown")
    lines.append("-" * 78)
    lines.append(
        f"  {'#':>3} {'top':>7} {'avg_fill':>9} {'shares':>9} {'notional':>10}  "
        f"throttle  question"
    )
    for i, r in enumerate(rows, start=1):
        lines.append(
            f"  {i:>3} {r.top_of_book_price:>7.4f} {r.depth_aware_avg_fill:>9.4f} "
            f"{r.shares:>9.2f} {r.notional_usd:>10.4f}  "
            f"{'YES' if r.book_exhausted else '   '}      {r.question}"
        )

    lines.append("")
    lines.append("kill-the-trade-if")
    lines.append("-" * 78)
    lines.append(
        f"  threshold: net gap after fees < {kill_bps:.1f} bps"
    )
    lines.append(
        f"  observed:  net gap = {net_gap_bps_used:+.2f} bps  ->  {go_label}"
    )
    if event.get("negRiskAugmented"):
        lines.append(
            "  note: event is negRiskAugmented — the sum-of-YES bound is not "
            "strictly 1.0 over the lifecycle (new outcomes can be added). The "
            "signal is still tradeable today, but lifetime arb cannot be locked."
        )
    if category.source == "default-politics":
        lines.append(
            "  note: no category tag matched — defaulted to Politics (1.00% fee) "
            "as a conservative upper bound. Pass --category to override."
        )
    lines.append("")
    lines.append("paste-into-TRADE_LOG block")
    lines.append("-" * 78)
    lines.append(
        f"  event:           {event.get('title')!r} ({event.get('slug')})"
    )
    lines.append(f"  category / fee:  {category.category} / {fee_bps_used:+.2f} bps")
    lines.append(f"  side:            {side}")
    lines.append(f"  exec mode:       {'maker' if maker else 'taker'}")
    lines.append(f"  total notional:  ${total_usd:.2f}  per-mkt ${per_mkt:.4f}")
    lines.append(
        f"  gap (top/depth): {basket_result.gap_top_of_book * 10_000:+.2f} bps top, "
        f"{basket_result.gap_depth_aware * 10_000:+.2f} bps depth-aware"
    )
    lines.append(
        f"  net gap:         {net_gap_bps_used:+.2f} bps "
        f"(after {fee_bps_used:+.2f} bps fee)"
    )
    lines.append(f"  E[P&L at settle]: {_fmt_usd(pnl_used)}")
    lines.append(f"  decision:        {go_label}")
    lines.append("=" * 78)
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> str:
    event = await load_event(args.slug)
    if not event.get("negRisk"):
        raise SystemExit(
            f"event {args.slug!r} is not negRisk; basket math does not apply. "
            "Pick a negRisk event (see `uv run polymarket-edge stats`)."
        )
    active = _active_markets(event)
    if len(active) < 2:
        raise SystemExit(f"event {args.slug!r} has <2 active markets; nothing to basket.")

    category = detect_category(event, args.category)
    signal = score_event(event)
    if signal is None:
        raise SystemExit(
            f"event {args.slug!r} could not be scored (missing quotes on a leg)."
        )

    side = args.side or autodetect_side(signal.bid_gap, signal.ask_gap)
    if side not in {"sell_yes", "buy_yes"}:
        raise SystemExit(f"--side must be sell_yes or buy_yes, got {side!r}")

    books = await book_depth.fetch_books_for_event(active)
    if not books:
        raise SystemExit("no order books returned; CLOB may be down. Retry.")

    per_market_usd = args.total_usd / len(active)
    if side == "sell_yes":
        basket = book_depth.basket_sell_yes_depth(
            active, books, notional_per_market_usd=per_market_usd
        )
        sum_other = signal.sum_best_ask
    else:
        basket = book_depth.basket_buy_yes_depth(
            active, books, notional_per_market_usd=per_market_usd
        )
        sum_other = signal.sum_best_bid

    rows = _per_market_breakdown(active, books, side, per_market_usd)
    return build_report(
        event=event,
        category=category,
        side=side,
        total_usd=args.total_usd,
        maker=args.maker,
        rows=rows,
        basket_result=basket,
        sum_top_other_side=sum_other,
        kill_bps=args.kill_bps,
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Size a single real $20-scale basket trade on a flagged Polymarket "
            "negRisk event. Outputs a paste-ready text report."
        ),
    )
    p.add_argument("--slug", required=True, help="Polymarket event slug")
    p.add_argument(
        "--total-usd", type=float, default=20.0,
        help="Total basket notional in USD (default $20)",
    )
    p.add_argument(
        "--side", choices=["sell_yes", "buy_yes"], default=None,
        help="Trade direction. Auto-detected from gap sign if omitted.",
    )
    p.add_argument(
        "--category", default=None,
        help=(
            "Override category. One of: "
            + ", ".join(sorted(TAKER_FEE_BPS))
            + ". Auto-detected from event tags otherwise."
        ),
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--maker", dest="maker", action="store_true", default=True,
                      help="Quote on the book as maker (default; 0%% fee + rebate)")
    mode.add_argument("--taker", dest="maker", action="store_false",
                      help="Cross the spread as taker (pay full category fee)")
    p.add_argument(
        "--kill-bps", type=float, default=DEFAULT_KILL_THRESHOLD_BPS,
        help=f"Abort trade if net gap < this many bps (default {DEFAULT_KILL_THRESHOLD_BPS})",
    )
    return p


def main() -> None:
    args = _build_parser().parse_args()
    report = asyncio.run(_run(args))
    print(report)


if __name__ == "__main__":
    main()
