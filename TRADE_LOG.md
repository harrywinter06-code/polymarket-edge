# Trade log — <event slug>

Fill this in within 24 hours of the last fill. Settlement section can be left as "pending" and updated when the event resolves.

## Pre-trade

- Date / time UTC:
- Event:
- Event slug:
- negRisk / negRiskAugmented:
- Side (sell_yes / buy_yes):
- Total USD risked:
- Maker vs taker (or simulation):
- Sizing script command (exact):
- Sizing script output (paste the full block):

```
<paste size_basket_trade.py output here>
```

- Depth-aware gap at sizing: X bps top-of-book, Y bps after walking
- Net gap after fee/rebate at chosen execution mode: Z bps
- Decision: GO / NO-GO and one-sentence reason

## Fills

| # | market | side | limit price | fill price | shares | fee / rebate USD | order id | timestamp UTC | notes |
|---|---|---|---|---|---|---|---|---|---|
| 1 |   |   |   |   |   |   |   |   |   |
| 2 |   |   |   |   |   |   |   |   |   |
| 3 |   |   |   |   |   |   |   |   |   |

(Add rows for every leg in the basket. For unfilled legs, write "no fill" in the fill price column and "0" in shares.)

Totals:

- Total shares purchased / sold across all legs:
- Total USD cash leg (sum of fill_price × shares):
- Total fees paid / rebates received (signed USD, rebates are negative):

Screenshots: link or attach the order-confirmation screenshots here.

## Realized (at settlement)

- Settled at (UTC date):
- Winning market:
- Shares held in winning market:
- Payout USD (= shares in winner × $1):
- Total fees / rebates net (carried from Fills section):
- **Realized P&L USD:** (cash leg) ± (payout) − (fees, net of rebates)
- Realized P&L vs sizing-script E[P&L] estimate (delta):

## What I learned

(Free text — 3-6 sentences. What surprised you? Did the depth-aware fill match the realized fill? Did partial fills change the basket composition meaningfully? Would you trade this signal again at 10× the size, and what would you do differently?)
