# Trade log

This log documents real fills on Polymarket flagged-event basket trades, per the runway in [EXECUTION.md](EXECUTION.md). Polymarket geoblocks the UK at both the venue (help.polymarket.com geographic restrictions) and the FCA-regulator level (binary-options ban for retail). I am physically in the UK; the live-fill section below is therefore empty pending a non-UK opportunity.

In place of a live fill, the **simulation path** from `EXECUTION.md §4b` produces real EIP-712-signed CLOB orders from a throwaway EOA. The simulation does not broadcast — the throwaway address holds no funds and is not associated with any funded Polymarket proxy — but the signatures are valid and would clear `clob.polymarket.com/order` validation if the address were funded. This is the closest credibility signal to "I have traded on this venue" available under the jurisdiction constraint, and it's a stronger artifact than a paper-only project that quietly claimed to have traded.

## Simulation run — 2026 FIFA World Cup negRisk event

- **Run timestamp (UTC):** 2026-05-25 10:09:03
- **Event:** 2026 FIFA World Cup Winner (`2026-fifa-world-cup-winner-595`)
- **negRisk / negRiskAugmented:** True / True
- **Detector verdict at run time:** sell_yes, top-of-book gap +0.0080 (+80 bps) — note: gap has compressed substantially since the build-window +150 bps capture; this run is a thinner snapshot and the World Cup remains the durable case-study event but the live signal is no longer the build-window magnitude
- **Side:** sell_yes
- **Total notional:** $5.00 across 5 attempted markets ($1.00/market)
- **Throwaway signing address:** `0x1e13a647380088d6Cfa4c8f5B654C4D9A2d63673`
- **Chain:** Polygon (chain_id 137)
- **Broadcast:** **No** (UK jurisdiction; throwaway address has no funded Polymarket proxy by construction)
- **Artifact:** `signed_orders/2026-fifa-world-cup-winner-595-20260525T100903Z.json` (gitignored by default; `git add -f` to commit if reviewer verification is desired)

### Signed orders (4 of 5 attempted)

| market | side | limit price | shares | EIP-712 sig valid? |
|---|---|---|---|---|
| Will Spain win the 2026 FIFA World Cup? | sell_yes | 0.1730 | 5.7803 | ✅ |
| Will Switzerland win the 2026 FIFA World Cup? | sell_yes | 0.0090 | 111.1111 | ✅ |
| Will England win the 2026 FIFA World Cup? | sell_yes | 0.1120 | 8.9286 | ✅ |
| Will France win the 2026 FIFA World Cup? | sell_yes | 0.1750 | 5.7143 | ✅ |
| Will New Zealand win the 2026 FIFA World Cup? | (skipped — no resting bid on flagged side at run time) | | | — |

One representative signed payload (Spain leg), for verification by any reviewer with `py-clob-client` or an EIP-712 verifier:

```json
{
  "order_values": {
    "salt": 438695170,
    "maker": "0x1e13a647380088d6Cfa4c8f5B654C4D9A2d63673",
    "signer": "0x1e13a647380088d6Cfa4c8f5B654C4D9A2d63673",
    "taker": "0x0000000000000000000000000000000000000000",
    "tokenId": "4394372887385518214471608448209527405727552777602031099972143344338178308080",
    "makerAmount": 5780000,
    "takerAmount": 999940,
    "expiration": 0, "nonce": 0, "feeRateBps": 0,
    "side": 1, "signatureType": 0
  },
  "signature": "0xe6f37a4bccb6b7bdf0f55b2841d1bd83fad6ae82d75d83c6a50930a47c29ed154b3998b484d4ef3eb08d8446693b286bd8f96fe68ecb3c99ab58dca62ee1051b1b"
}
```

Promoting this from simulation to live = call `client.post_order(signed_order, GTC)` with a real funded key from a non-restricted jurisdiction.

---

## Live fill template (pending non-UK opportunity)

The fields below are the structured log for an eventual live fill. Settlement can be left as "pending" and updated when the event resolves.

### Pre-trade

- Date / time UTC:
- Event:
- Event slug:
- negRisk / negRiskAugmented:
- Side (sell_yes / buy_yes):
- Total USD risked:
- Maker vs taker:
- Sizing script command (exact):
- Sizing script output (paste the full block):

```
<paste size_basket_trade.py output here>
```

- Depth-aware gap at sizing: X bps top-of-book, Y bps after walking
- Net gap after fee/rebate at chosen execution mode: Z bps
- Decision: GO / NO-GO and one-sentence reason

### Fills

| # | market | side | limit price | fill price | shares | fee / rebate USD | order id | timestamp UTC | notes |
|---|---|---|---|---|---|---|---|---|---|
| 1 |   |   |   |   |   |   |   |   |   |

(Add rows for every leg in the basket. For unfilled legs, write "no fill" in the fill price column and "0" in shares.)

Totals:

- Total shares purchased / sold across all legs:
- Total USD cash leg (sum of fill_price × shares):
- Total fees paid / rebates received (signed USD, rebates are negative):

### Realized (at settlement)

- Settled at (UTC date):
- Winning market:
- Shares held in winning market:
- Payout USD (= shares in winner × $1):
- Total fees / rebates net (carried from Fills section):
- **Realized P&L USD:** (cash leg) ± (payout) − (fees, net of rebates)
- Realized P&L vs sizing-script E[P&L] estimate (delta):

### What I learned

(Free text — 3-6 sentences. What surprised you? Did the depth-aware fill match the realized fill? Did partial fills change the basket composition meaningfully? Would you trade this signal again at 10× the size, and what would you do differently?)
