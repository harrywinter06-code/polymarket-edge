# Execution checklist — one real $20 basket trade

This document is the runway for placing **one** real-money $20 basket trade on a flagged Polymarket negRisk event. The point is not the dollars. The point is having a real fill, with a real fee, on a real order book — attribution data that turns the project from "paper-only backtest" into "I have actually executed on this venue, here is the log." REDTEAM.md §1 calls out "nothing has been traded yet" as the project's biggest credibility ceiling. This is how that gets fixed.

Hard rules:

- $20 total notional across the entire basket. Not $20 per market. On the World Cup event that is $0.42 per market across 48 markets.
- This is a learning trade, not an income trade. The expected P&L at maker pricing is on the order of a few cents; the realized P&L will be dominated by fill timing, partial fills, and the random which-market-wins draw. If it loses $5 that is tuition.
- Do not violate Polymarket jurisdiction restrictions. See §0 below.

## 0. Jurisdiction check — UK is restricted (do this first)

Polymarket geoblocks the UK at the platform level. Polymarket's own Help Center page on Geographic Restrictions lists the UK among the restricted jurisdictions, and the FCA's 2019 binary-options ban for retail consumers covers any contract that settles to $1 or $0 — which is exactly the negRisk YES contract. Sources verified 2026-05-21:

- Polymarket Help Center, "Geographic Restrictions" — `help.polymarket.com/en/articles/13364163-geographic-restrictions`
- Datawallet, "Polymarket Supported and Restricted Countries (2026)"
- Predmarket.io, "Is Polymarket Legal in the UK? (2026)"

If you are physically in the UK at the time of execution, **do not deposit and do not place a live trade**. The alternative — which still produces useful attribution data for the application — is the simulation path in §4b below.

If you are temporarily outside the UK in a non-restricted jurisdiction (e.g. travel, holiday) the live path in §4a is open. In either case, do not use a VPN to bypass geoblocking; doing so violates Polymarket's Terms of Service and any resulting trade is at risk of being voided.

## 1. Pre-flight

You need:

- A Polymarket account in good standing in a non-restricted jurisdiction (or the simulation path below).
- A wallet you control on Polygon (the Polymarket UI provisions one for you via email login if you don't already have a self-custodial wallet).
- **~$25 of USDC on the Polygon network** in that wallet — $20 for the trade, ~$5 for slippage buffer and tiny MATIC gas.
- A small amount of MATIC for gas. Polymarket's UI typically handles this via meta-transactions, but if you place orders via py-clob-client you need MATIC.

Deposit walkthrough — official docs:

- Polymarket Documentation, "How to Deposit": `docs.polymarket.com/polymarket-learn/get-started/how-to-deposit`
- Polymarket Help Center, "How to Deposit": `help.polymarket.com/en/articles/13369887-how-to-deposit`

The deposit must be USDC on **Polygon**. Sending USDC on Ethereum mainnet, or USDT on any chain, results in permanently lost funds.

## 2. Run the sizing script

From the repo root:

```powershell
$env:PYTHONPATH = "src"
python scripts/size_basket_trade.py --slug 2026-fifa-world-cup-winner-595 --total-usd 20 --maker
```

The script auto-detects the category from the event's `tags` (Sports for the World Cup → 0.75% taker fee), auto-picks the side from the top-of-book gap sign (positive bid-gap → sell_yes), fetches the full CLOB order book on every active market in the event, and prints:

- Top-of-book sum and gap
- Depth-aware basket fill at $20 / n_markets per market
- Maker vs taker net gap (after fees / plus rebate) and expected $ P&L
- A per-market breakdown with fill price, shares, and book-exhaustion flag per market
- A "kill-the-trade-if" line that prints `GO` or `NO-GO` against the default 30 bps net threshold
- A paste-into-TRADE_LOG block with the headline numbers

If the slug has gone stale (the event closed, was archived, or the gap compressed below the detector threshold), re-run with another flagged slug. Find current candidates with `uv run polymarket-edge stats`.

## 3. Go / no-go

The script prints a decision line. Follow it strictly:

- `GO` (net gap ≥ 30 bps after fees): proceed to §4.
- `NO-GO` (net gap < 30 bps): do not execute. The signal has compressed since the build snapshot. Either pick a different flagged slug or wait. The whole point of the kill-line is to prevent firing a trade just to fire a trade.

Two additional auto-aborts:

- The script raises a clear error if the slug doesn't match any active event.
- If the event is `negRiskAugmented: true`, the script prints a warning that the sum-of-YES bound is not strictly 1.0 over the full lifecycle. The signal is still tradeable today (the basket settles when the event resolves), but you should not interpret it as a guaranteed lock.

## 4a. Execute as a MAKER on the Polymarket UI (default)

Maker pricing is the right default for this trade: it pays 0% fee and earns a 20–25% rebate on the bid-ask spread crossed by the taker who fills you. The expected E[P&L] in the script reflects that.

1. Open the event page (`polymarket.com/event/2026-fifa-world-cup-winner-595`).
2. For each of the constituent markets in your basket (or a representative subset if you want a smaller leg count — the script handles the math at $20 total):
   - Click "Sell YES" (sell-side) or "Buy YES" (buy-side) per the script's `side` field.
   - Enter the per-market notional from the script (e.g. $0.42).
   - Select **Limit** order type.
   - Set the limit price to the current best bid (sell-side) or best ask (buy-side) — i.e. quote *at* top of book rather than crossing. This makes you the maker.
   - Place the order. Note the order ID from the confirmation toast.
3. The order sits on the book until a taker hits it. For a $0.42 leg this typically takes seconds to minutes on the liquid markets in the World Cup event, longer on the thin legs. Some legs may not fill at all in your trading window — that is fine, log the unfilled ones.

Taker fills are also acceptable but reduce the trade to a "we used taker just to fire" learning exercise. If you go taker, mark it in TRADE_LOG.md.

Advanced path: place orders via py-clob-client's order-builder. The library produces a signed L1 order payload you can broadcast via the CLOB endpoint. Only useful if you want the order ID, fill timestamps, and fee fields programmatically rather than via screenshots.

## 4b. Simulation path (UK / restricted-jurisdiction users)

If you cannot legally trade live, use py-clob-client's order-builder to *construct* a signed order payload at the same prices the script suggests, but do not broadcast. Save the signed payload, the order parameters, and the live order-book snapshot from step 2 — together they form an "as-if" trade that demonstrates the full execution path was understood and runnable, without breaching jurisdiction restrictions. Document this explicitly in TRADE_LOG.md under "Maker vs taker" as "simulation — payload constructed, not broadcast."

This is honestly weaker as a credibility signal than a real fill, but it is honest, and the Ask Gina reviewer will respect it more than a paper-only project that quietly claimed to have traded.

## 5. Capture immediately after each fill

For every filled leg, write down (or screenshot):

- Market question (e.g. "Will Brazil win the 2026 FIFA World Cup?")
- Side (sell_yes / buy_yes)
- Fill price
- Number of shares filled (and partial-fill flag if not fully filled)
- Fee paid or rebate received in USD
- Order ID
- Timestamp (UTC)

Screenshot the order confirmation panel. These go into TRADE_LOG.md.

## 6. Monitor and exit

The 2026 World Cup is a long-dated event (resolves after the tournament ends in summer 2026). For a $20 basket the right exit is to let it ride to settlement — costs to exit early would dominate the realized P&L. If the event were short-dated (e.g. Weinstein sentencing), the same applies at this size; do not get clever with early exits on a $20 trade.

Two early-exit triggers worth knowing exist but are unlikely to fire at $20:

- The depth-aware gap re-widens significantly — you could add to the position. At $20 scale this is not worth the gas / fee overhead.
- A news shock changes the structure of the event (e.g. a new outcome added in an augmented event). Document it in TRADE_LOG.md but do not panic-exit.

When the event resolves:

- One market pays $1 per share to YES holders; all others pay $0.
- For sell-side, you received cash up front and pay $0 on the losers, $1 per share on the one winner.
- Net realized P&L = (sum of cash received at fill) − (1 × shares held in winning market) − (sum of fees, net of rebates).

## 7. Post-trade

Fill in TRADE_LOG.md within 24 hours of the last fill, even if settlement is months away. Note "settlement pending" in the realized section; come back to fill in the realized P&L when the event resolves.
