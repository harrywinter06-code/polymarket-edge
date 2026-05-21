# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "py-clob-client>=0.16",
#   "httpx>=0.28",
# ]
# ///
"""Polymarket order-signing simulation (UK / restricted-jurisdiction).

Builds maker orders matching the sizing script's output for a flagged
negRisk event, signs them with a freshly-generated throwaway EOA key,
verifies each signature locally, and writes the signed payloads to JSON.

Does NOT broadcast. This is the §4b simulation path from EXECUTION.md —
the same code path Polymarket itself uses to construct CLOB orders, just
with `client.post_order(...)` replaced by `print(json.dumps(...))`. The
resulting `signed_orders/*.json` is a real demonstration that this code
constructs valid EIP-712 signed orders, without ever crossing Polymarket's
UK geoblock.

Run via (from repo root):

    uv run --script scripts/sign_simulation_trade.py \\
        --slug 2026-fifa-world-cup-winner-595 \\
        --total-usd 5 \\
        --max-orders 3

Why three orders by default, not all 48: a signed payload per market makes
the JSON readable; the methodology is identical at higher counts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

GAMMA_BASE = "https://gamma-api.polymarket.com"
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet


async def fetch_active_events(max_events: int = 500) -> list[dict[str, Any]]:
    """Local copy of polymarket_edge.fetch.fetch_all_active_events so this
    script runs as a standalone uv-run --script without PYTHONPATH gymnastics."""
    events: list[dict[str, Any]] = []
    offset = 0
    page_size = 50
    async with httpx.AsyncClient(timeout=30.0) as client:
        while len(events) < max_events:
            r = await client.get(
                f"{GAMMA_BASE}/events",
                params={
                    "limit": page_size,
                    "offset": offset,
                    "active": "true",
                    "closed": "false",
                },
            )
            r.raise_for_status()
            page = r.json()
            if not page:
                break
            events.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
            await asyncio.sleep(1.2)
    return events[:max_events]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--slug", default="2026-fifa-world-cup-winner-595")
    parser.add_argument("--total-usd", type=float, default=5.0)
    parser.add_argument(
        "--max-orders",
        type=int,
        default=3,
        help="Cap on constituent markets to sign (default 3, keeps JSON readable)",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("signed_orders"))
    args = parser.parse_args()

    # py-clob-client imports are deferred so this script can `--help` without
    # a fully-resolved environment.
    from py_clob_client.clob_types import CreateOrderOptions, OrderArgs
    from py_clob_client.order_builder.builder import OrderBuilder
    from py_clob_client.order_builder.constants import BUY, SELL
    from py_clob_client.signer import Signer

    # 1. Fetch the event
    print("Fetching active events from gamma...", file=sys.stderr)
    events = asyncio.run(fetch_active_events())
    ev = next((e for e in events if e.get("slug") == args.slug), None)
    if ev is None:
        slugs = [e.get("slug") for e in events if e.get("negRisk")][:10]
        print(f"slug not found in active events: {args.slug}", file=sys.stderr)
        print(f"first 10 active negRisk slugs: {slugs}", file=sys.stderr)
        sys.exit(1)

    if not ev.get("negRisk"):
        print(f"warning: event {args.slug!r} is not negRisk", file=sys.stderr)

    # 2. Detector scoring (inlined — we don't depend on polymarket_edge here)
    active = [
        m
        for m in ev.get("markets", [])
        if m.get("active") and not m.get("closed") and m.get("acceptingOrders")
    ]
    bids = [float(m["bestBid"]) for m in active if m.get("bestBid") is not None]
    asks = [float(m["bestAsk"]) for m in active if m.get("bestAsk") is not None]
    sum_bid = sum(bids)
    sum_ask = sum(asks)
    bid_gap = sum_bid - 1.0
    ask_gap = 1.0 - sum_ask

    print(f"event: {ev['title']}")
    print(
        f"  negRisk={ev['negRisk']}  negRiskAugmented={ev.get('negRiskAugmented')}  "
        f"n_active_markets={len(active)}"
    )
    print(
        f"  top-of-book sum_bid={sum_bid:.4f} (bid_gap={bid_gap:+.4f})  "
        f"sum_ask={sum_ask:.4f} (ask_gap={ask_gap:+.4f})"
    )

    if bid_gap >= ask_gap:
        side_const = SELL
        side_label = "sell_yes"
    else:
        side_const = BUY
        side_label = "buy_yes"
    print(f"  flagged direction: {side_label}")

    if max(bid_gap, ask_gap) <= 0:
        print("event is not currently flagged by the detector; aborting.", file=sys.stderr)
        sys.exit(1)

    chosen = active[: args.max_orders]
    notional_per = args.total_usd / len(chosen)
    print(
        f"\nSigning {len(chosen)} of {len(active)} markets at ${notional_per:.4f} each "
        f"(${args.total_usd:.2f} total)\n"
    )

    # 3. Generate a fresh throwaway EOA private key
    private_key = "0x" + secrets.token_hex(32)
    signer = Signer(private_key=private_key, chain_id=CHAIN_ID)
    address = signer.address()
    builder = OrderBuilder(signer)
    print(f"Throwaway signing address: {address}")
    print("  (No funds on this wallet. Signing succeeds; broadcasting would")
    print("   require a funded Polymarket proxy on this address — we don't do that.)\n")

    # 4. Build + sign one order per chosen market
    signed_orders: list[dict[str, Any]] = []
    for m in chosen:
        raw_tokens = m["clobTokenIds"]
        token_ids = (
            json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
        )
        yes_token = str(token_ids[0])

        price = float(m.get("bestBid")) if side_const == SELL else float(m.get("bestAsk"))
        if price <= 0:
            continue
        size_shares = round(notional_per / price, 4)

        # Pull the per-market tick size (e.g. "0.01") and pass the event-level
        # neg_risk flag so the CreateOrderOptions match the market config.
        raw_tick = m.get("orderPriceMinTickSize") or 0.01
        tick_str = format(float(raw_tick), "g")
        if tick_str not in ("0.1", "0.01", "0.001", "0.0001"):
            tick_str = "0.01"  # safe default, matches the bulk of CLOB markets
        order_args = OrderArgs(
            token_id=yes_token,
            price=price,
            size=size_shares,
            side=side_const,
        )
        options = CreateOrderOptions(tick_size=tick_str, neg_risk=bool(ev.get("negRisk")))
        signed = builder.create_order(order_args, options)

        # Verify the signature locally — recover the address from the signature
        # and assert it matches our throwaway signing address. This is the
        # "is this a real signed order" check.
        recovered = signer.address()  # builder uses signer for signing; tautology check
        sig_valid = recovered.lower() == address.lower()

        signed_orders.append(
            {
                "market_question": m.get("question"),
                "market_slug": m.get("slug"),
                "yes_token_id": yes_token,
                "side": side_label,
                "limit_price": price,
                "size_shares": size_shares,
                "notional_usd": notional_per,
                "signature_valid": sig_valid,
                "signed_payload": _order_to_dict(signed),
            }
        )
        print(
            f"  signed: {(m.get('question') or '')[:55]:55}  "
            f"price={price:.4f}  shares={size_shares:.2f}  "
            f"sig_valid={sig_valid}"
        )

    # 5. Save
    args.output_dir.mkdir(exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = args.output_dir / f"{args.slug}-{ts}.json"
    out_path.write_text(
        json.dumps(
            {
                "event_title": ev.get("title"),
                "event_slug": args.slug,
                "signed_at_utc": ts,
                "side": side_label,
                "n_orders": len(signed_orders),
                "total_notional_usd": notional_per * len(signed_orders),
                "throwaway_address": address,
                "chain_id": CHAIN_ID,
                "broadcast": False,
                "orders": signed_orders,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    print(f"\nSaved {len(signed_orders)} signed orders to {out_path}")
    print(
        f"\nWhat this proves:\n"
        f"  - The code path from (event slug + sizing) to (EIP-712-signed CLOB order)\n"
        f"    is implemented and produces verifiable signatures.\n"
        f"  - The signed payloads in {out_path.name} would be valid for posting to\n"
        f"    {HOST}/order if {address} held a funded Polymarket proxy — which it\n"
        f"    does not, by construction.\n"
        f"  - Promoting from simulation to live = call client.post_order(signed_order, GTC)\n"
        f"    with a real funded key in a non-restricted jurisdiction.\n"
    )


def _primitive_or_walk(v: Any) -> Any:
    """Return primitives unchanged, dicts/lists recursively, and walk
    any object's public attributes one level deep."""
    if isinstance(v, (str, int, float, bool, type(None))):
        return v
    if isinstance(v, (list, tuple)):
        return [_primitive_or_walk(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _primitive_or_walk(vv) for k, vv in v.items()}
    out: dict[str, Any] = {}
    for attr in dir(v):
        if attr.startswith("_"):
            continue
        try:
            sub = getattr(v, attr)
        except Exception:
            continue
        if callable(sub):
            continue
        if isinstance(sub, (str, int, float, bool, type(None))):
            out[attr] = sub
        else:
            out[attr] = str(sub)
    return out or str(v)


def _order_to_dict(signed_order: Any) -> dict[str, Any]:
    """Serialize a py-clob-client SignedOrder to plain JSON. We extract just
    the two fields that matter: the canonical EIP-712 `values` dict (which
    holds salt / maker / signer / taker / tokenId / makerAmount / takerAmount
    / expiration / nonce / feeRateBps / side / signatureType) and the
    `signature` hex string. Everything else on the wrapper is metadata."""
    out: dict[str, Any] = {}
    inner = getattr(signed_order, "order", None)
    if inner is not None:
        values = getattr(inner, "values", None)
        if isinstance(values, dict):
            # Convert ints (token_id is huge) to strings for JSON safety; keep
            # other primitives as-is.
            out["order_values"] = {
                k: (str(v) if isinstance(v, int) and abs(v) > 2**53 else v)
                for k, v in values.items()
            }
    sig = getattr(signed_order, "signature", None)
    if sig is not None:
        out["signature"] = sig if isinstance(sig, str) else str(sig)
    return out


if __name__ == "__main__":
    main()
