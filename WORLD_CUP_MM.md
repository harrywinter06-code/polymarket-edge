# 2026 FIFA World Cup — maker-side P&L simulation

A market-maker P&L projection on the 48-market 2026 FIFA World Cup negRisk
basket, netting the 18.75 bps Sports maker rebate against modelled adverse
selection, projected forward to tournament finals (~50 days).

## The question

The microstructure pass found the World Cup is the only `real` (depth-clearing
at $500/market) signal in the dataset by dollar weight — 95.9% of flagged
volume sits on this one event. A top-of-book gap exists, but it only matters
if someone can actually monetise it. This sim answers: **what would a maker
running passively across all 48 constituent markets earn per day, net of the
maker rebate, net of realistic adverse selection?**

## Method

**Trade data.** Public ``data-api.polymarket.com/trades`` filtered by
``market=<conditionId>``. Spec-best-guess of ``clob.polymarket.com/trades`` is
auth-gated (401); the data-api endpoint is public but caps pagination at
offset=3000. For the high-volume markets (Spain, France, etc.) this is
~4 days of history; for long-tail markets (Qatar, Iran, etc.) it is the full
30-day lookback. 77,510 trades pulled across 48/48 markets.

**Maker-fill estimation.** Assume the maker is the resting counterparty on a
configurable fraction of observed trades — default 0.5, sensitivity at 0.25
and 0.75. Gross rebate per captured trade = trade_notional × 18.75 / 10,000
(25% of the 0.75% Sports taker fee).

**Adverse-selection model.** Estimated half-spread per market = mean absolute
price change over a 5-min window following each trade, divided by 2. Mean (not
median) because Polymarket prices are quantised at $0.01 and many 5-min
windows show zero realised drift at the median. AS cost per captured trade =
trade_notional × (half_spread / mean_price) × scenario.realized_half_spread_fraction.

Three scenarios: **naive** (0.0 × spread — pure rebate), **moderate** (0.5 ×
spread — textbook MM literature) and **informed** (1.0 × spread —
pessimistic).

**Projection.** Per-day net × 50 days to tournament finals. Conservative
because flow is likely *higher* approaching the tournament; the projection
assumes the recent observed rate.

## Results

Capture fraction = 0.5, 48 markets, observed window 8.32 days (Brazil/England
saw the longest spans; Spain/France saw only 3-4 days of history within the
3000-trade cap).

| scenario | gross rebate | AS cost | net | per-day | **projected 50d** |
|---|---|---|---|---|---|
| naive | $2,060 | $0 | $2,060 | $247.44 | **+$12,372** |
| moderate | $2,060 | $2,039 | $20.97 | $2.52 | **+$126** |
| informed | $2,060 | $4,078 | −$2,018 | −$242.41 | **−$12,120** |

**Headline: moderate, projected to 50 days = +$126.** Barely positive.

Sensitivity to maker capture fraction (moderate AS): per-day P&L scales
linearly — $1.26/$2.52/$3.78 at capture 0.25/0.50/0.75.

**Breakeven half-spread fraction: 0.505.** Net P&L crosses zero almost
exactly at the moderate scenario. The strategy needs AS to be below ~50% of
the half-spread to clear.

## Per-market breakdown (moderate AS)

**Top 5 contributors carry 89% of the positive net P&L:**

| market | net | per-day | trades |
|---|---|---|---|
| France | +$207 | +$58.54 | 3,219 |
| Spain | +$178 | +$42.09 | 2,941 |
| England | +$139 | +$17.94 | 2,693 |
| Argentina | +$125 | +$14.97 | 2,358 |
| Brazil | +$103 | +$14.00 | 2,445 |

**41 of 48 markets are net-negative under moderate AS** — totalling −$824
collectively. The basket clears positive only because the top contenders
dominate the rebate side. Bottom 5 markets (Austria, Japan, Senegal,
Australia, Croatia) lose −$311 between them.

## Why it's barely positive

A maker rebate of 18.75 bps is structurally small. On a market with mean
price ~$0.10 and a 5-min mean absolute drift of ~$0.01, the half-spread is
$0.005 — *a 5% fraction of price*. At moderate AS (charging half the
spread) the cost per fill is ~2.5% of notional, while the rebate pays
0.1875%. Most markets lose. The big-favourite markets (Spain/France with
mean price ~$0.18-0.25) have *higher* notional per trade but *lower* spread
as a fraction of price, so they clear the rebate. The long tail at $0.01-
0.03 has 5-min drifts that are several bps of price and gets eaten.

## Caveats

- **AS model is the load-bearing assumption.** Realised price drift is a
  proxy for spread, not the spread itself. A true bid-ask spread series would
  give a tighter estimate. The three-scenario report and the breakeven
  fraction make the dependence explicit.
- **Pagination cap.** 3000-trade offset truncates the lookback for the
  high-volume markets to ~4 days. The per-day rate is computed over the
  longest observed market (8.3d, Argentina). If recent activity is unusually
  intense the per-day is biased high; if unusually quiet, biased low. The
  projection inherits this.
- **50-day linear projection** assumes the observed rate holds. Volume tends
  to spike near tournaments, so this is conservative for naive/moderate and
  optimistic for informed.
- **No competition for the rebate.** The 0.5 capture assumption is an
  abstraction over what is actually a queue: a hypothetical "sole maker" never
  exists, and competing makers eat each other's rebate.

## Bottom line

Microstructure trap-rate finding said: only the World Cup is durable. This
sim says: even on that durable signal, the maker-rebate strategy is
**knife-edge-positive**. The conclusion is not "deploy capital here";
it's **"the rebate floor is structurally tight on sports — at AS > 50%
of spread you lose, and 41/48 of the long-tail constituent markets are
net-negative individually."** That's a defensible risk-disclosure read
drawn directly from these numbers.
