# polymarket-edge — research note

Generated: 2026-05-21 06:16

## What we built and why

The Ask Gina quant-intern job description names Polymarket and Hyperliquid
as the venues. This project is a forward-observation + funding-capture stack
across both, designed in five days from a verified read of each venue's
public API.

Headline design decision: on Polymarket, `P(YES) + P(NO) = $1` is
contract-enforced per market via the CLOB order-mirroring rule, so naive
intra-market "yes + no > 1" arbs cannot exist in steady state. The
non-trivial signal lives at the **event** level — for a `negRisk` event with
N mutually-exclusive markets, the sum of YES probabilities across the event
must equal 1.0. Any deviation is potential arb (modulo fees).

## Data collected

| table | rows |
|---|---|
| events | 193 |
| markets | 1,440 |
| market_snapshots | 1,440 |
| event_arb_signals | 36 |
| signal_trajectories | 720 |
| hl_funding_history | 18,500 |
| paper_positions | 11 |

## Polymarket — event-level no-arb signals

**Across all scans:**

| metric | value |
|---|---|
| signals scored | 36 |
| mean best_gap | -0.0120 |
| max best_gap | 0.0150 |
| n signals over 50bp | 6 |
| n signals over 2% (fee-clearable) | 0 |

**Top flagged events (best_gap >= 50bp, dedup-by-event):**

| title | n_markets | direction | best_gap | flags |
|---|---|---|---|---|
| 2026 FIFA World Cup Winner  | 48 | sell_yes | +0.0150 |  |
| Which party wins 2028 US Presidential Election? | 2 | buy_yes | +0.0100 |  |
| Harvey Weinstein prison time? | 6 | sell_yes | +0.0080 |  |

## Polymarket — forward observation (persistence study)

**Observation window:** 2026-05-21T03:43:04 -> 2026-05-21T03:51:28  (666 trajectory rows)

| metric | value |
|---|---|
| snapshots | 666 |
| distinct events | 111 |
| mean abs(gap) | 0.0207 |
| p50 abs(gap) | 0.0100 |
| p90 abs(gap) | 0.0510 |
| p99 abs(gap) | 0.2355 |
| max abs(gap) | 0.4790 |

**Distinct events that ever crossed each threshold during the window:**

| threshold | n distinct events |
|---|---|
| 0.0050 | 85 |
| 0.0100 | 68 |
| 0.0200 | 26 |
| 0.0500 | 12 |

**Forward-test (entry on |gap| >= 50bp, hold >= 5 minutes):**

| metric | value |
|---|---|
| candidate entries | 246 |
| mean realized gap at close | -0.0102 |
| mean decay toward zero | +0.0006 |

Interpretation: a positive `mean decay toward zero` means flagged signals
revert toward fair pricing over the hold horizon — consistent with a real
microstructure inefficiency that is being arbed away by the time fees clear it.


## Hyperliquid — funding-capture backtest

**Dataset:** 18,500 hourly funding ticks across 37 coins.

**Per-coin annualized funding (top 10 by mean realized rate):**

| coin | annualized mean | annualized vol |
|---|---|---|
| FARTCOIN | +17.7% | 0.3% |
| AZTEC | +14.5% | 0.8% |
| DOOD | +12.0% | 0.1% |
| kBONK | +11.9% | 0.1% |
| HEMI | +11.4% | 0.4% |
| HMSTR | +11.4% | 0.3% |
| LINEA | +11.3% | 0.0% |
| kPEPE | +10.9% | 0.1% |
| DOGE | +10.8% | 0.0% |
| LINK | +10.7% | 0.1% |

**Strategy results (rebalance 8h, top-K = 5, trailing window = 24h):**

| strategy | n_rebalances | total | annualized | ann_vol | sharpe | mdd | hit |
|---|---|---|---|---|---|---|---|
| top5_trail24h_rebal8h | 56 | +0.0097 | +0.1903 | 0.0051 | +36.98 | 0.0001 | 98.2% |
| perfect_top5_rebal8h | 59 | +0.0120 | +0.2229 | 0.0056 | +39.65 | 0.0000 | 100.0% |
| passive_short_BTC_rebal8h | 62 | +0.0013 | +0.0229 | 0.0017 | +13.43 | 0.0005 | 66.1% |

The trailing-mean predictor captures **81%** of the
perfect-hindsight ceiling. The Sharpe numbers here are an upper bound — they
do not include the cost of the spot hedge leg, basis risk, slippage, or
liquidation-buffer drag. Realistic net returns are materially lower.


## Live paper-trading

| metric | value |
|---|---|
| open positions | 11 |
| closed positions | 0 |
| total realized P&L (USD) | +0.00 |
| mean realized P&L per closed (USD) | +0.0000 |
| gross closed notional (USD) | 0.00 |

Position P&L model: `pnl = notional * (|entry_gap| - |current_gap|)`.
This is the linear-approximation P&L for the underlying basket trades and
does not include taker fees, slippage, or hold-to-settlement reset.


## Honest limitations

The numbers in this note overstate net realizable P&L. Specifically:

- **Polymarket**:
  - The detector treats `negRisk: true` events as mutually exclusive **and
    exhaustive**, but `negRiskOther` markets break exhaustivity. Events with
    `has_neg_risk_other = True` should be discounted accordingly.
  - Quote-fill assumption is `best_bid` / `best_ask` (top-of-book). Real
    fills cross the book, especially on the illiquid lower-probability legs
    of a multi-outcome event.
  - Taker fees are typically ~2% per leg; combined with hedge-leg drag, only
    >2% gaps are likely to clear in practice. None of the live signals during
    the observation window cleared that bar — they topped out around 150bp.
  - Historical retrospective is blocked by the CLOB `/prices-history`
    12h-granularity floor on resolved markets, so we cannot reconstruct the
    exact intra-day path of past mispricings.

- **Hyperliquid**:
  - The backtest measures funding flows only. It does NOT model the spot
    hedge leg cost (basis risk, spot funding, slippage), the leverage /
    liquidation buffer drag, or transaction fees. Reported Sharpe is an
    upper bound that real net returns will fall well short of.
  - Coin selection relies on `open_interest` from current snapshot, which is
    measured in token units, not USD notional — the default selector skews
    toward memecoins. For majors (BTC/ETH/SOL etc.) the funding rate has a
    floor at the 10.95% APR base rate.
  - 30 days of history is a small sample. Sharpe on small N is noisy and the
    universe composition has structural shifts (new perp listings, OI
    shocks) that the backtest does not adjust for.

- **Paper-trading**:
  - P&L is linear-approximation in the gap, not the true sum-of-prices math
    accounting for fees and settlement timing.

## What would be next

- Polymarket: account for `negRiskOther` in the sum constraint; pull
  `/prices-history` at 12h fidelity for resolved `negRisk` events and chart
  the time-series of `sum(best_bid)` over each event's lifecycle.
- Hyperliquid: pair funding capture with a real spot/perp hedge model; pull
  spot prices and compute the realized hedge P&L per period.
- Cross-venue: pair Polymarket binary outcomes that are statistically linked
  to onchain assets (e.g., regulatory-decision markets vs BTC funding skew)
  and test for joint mispricings.
