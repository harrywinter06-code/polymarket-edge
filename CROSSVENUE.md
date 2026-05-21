# Cross-venue case study: Polymarket Fed-cuts market vs Hyperliquid BTC perp

This is the dropped-ambition leg of the project, picked up as an honest, single-pair
investigation. The red-team flagged "no cross-venue work" as the build's biggest gap.
This document is the answer: one pair, one method, one specific result.

## What we tested

**Pair.** Polymarket YES token for *"Will no Fed rate cuts happen in 2026?"* (the
single most-actively-traded outcome in the `how-many-fed-rate-cuts-in-2026` event, a
`negRisk` market with $28.7M lifetime volume as of the run date) against the Hyperliquid
BTC-USD perpetual mark (close of hourly candle). The PM token's price is the
market-implied probability that the FOMC holds rates flat for all of 2026 — the cleanest
single-token proxy for the *hawkish-Fed* scenario.

**Hypothesis.** If Fed easing expectations propagate to crypto via the risk-on channel,
an upward move in P(no cuts) — a hawkish surprise — should be associated with weakness
in BTC, contemporaneously or with a short lead. We test both directions: PM-leads-HL
would suggest informed macro traders price the Fed outcome on Polymarket before crypto
moves; HL-leads-PM would suggest crypto's deeper liquidity and faster microstructure
discount the macro before the prediction-market book catches up.

## Data and method

30 days of data ending 2026-05-21: 718 hourly Polymarket points (this is an active
market — the CLOB returns hourly fidelity when the question is still live) and 721
hourly Hyperliquid BTC candle closes. Both legs are bucketed to **12-hour windows**,
keyed by absolute epoch (`floor(t / 12h) * 12h`) so two independent runs produce the
same grid. Within each bucket the last observation wins. This 12h floor is the project's
self-imposed worst-case assumption: the CLOB `/prices-history` endpoint is documented
to floor at 12h granularity for *resolved* markets per
[py-clob-client#216](https://github.com/Polymarket/py-clob-client/issues/216), and any
historical replay of this pair after the question resolves will be constrained that way.
Computing the analysis at 12h now matches the resolution any retrospective backtest
would inherit.

Per bucket we record `pm_delta` (raw PM probability change, in [-1, +1] units) and
`hl_log_return = log(close_t / close_{t-1})`. We then compute Pearson correlation
between these two series at integer lags from -4 to +4 buckets (i.e. -2 days to +2 days).
**Positive lag means PM leads HL.**

Limitations on the method: (i) 12h granularity blinds us to anything that resolves
inside a half-day, including the immediate FOMC-announcement reaction; (ii) N=60
overlapping buckets gives a standard error on r of approximately
1/√60 ≈ 0.13, so any single correlation under ~0.26 is within a 2σ noise band;
(iii) we test 9 lags, so the family-wise threshold for "real" is meaningfully tighter
than the per-lag threshold.

## Results

Window: 2026-04-21 12:00 UTC → 2026-05-21 12:00 UTC. 61 aligned 12h buckets.
PM probability ranged 0.340 → 0.721 (real signal, not a stuck market). BTC mark ranged
$75,727 → $82,479.

| lag (12h buckets) | meaning              | Pearson r |
|------------------:|----------------------|----------:|
| -4                | HL leads PM by 4     |   -0.0451 |
| -3                | HL leads PM by 3     |   -0.1096 |
| -2                | HL leads PM by 2     |   -0.1154 |
| -1                | HL leads PM by 1     |   -0.0961 |
|  0                | contemporaneous      |   -0.0629 |
| +1                | PM leads HL by 1     |   +0.0651 |
| +2                | PM leads HL by 2     |   -0.1030 |
| +3                | PM leads HL by 3     |   +0.2410 |
| +4                | PM leads HL by 4     |   +0.0677 |

**Headline numbers.** At lag=0, r = **-0.063**. The maximum absolute correlation
across all nine lags is r = **+0.241** at lag = +3 (PM leads HL by 36 hours).

## Interpretation

This is a null. The contemporaneous correlation is statistically indistinguishable
from zero (|r| = 0.063, well under the 0.13 per-lag SE). The largest |r| in the table is
0.241 at lag=+3, which is roughly 1.9σ on a single test — but we tested 9 lags, and a
Bonferroni-adjusted threshold to keep family-wise alpha at 0.05 needs |r| ≳ 0.36 with
N≈60. The +3 number does not clear that bar. It is the kind of value you should expect
from 9 independent draws of noise.

The sign pattern is also not what the risk-on thesis predicts. The thesis says
P(no-cuts) rising → BTC weakening, i.e. *negative* correlation at lag 0 or +1. The
table shows the negative-correlation cluster on the HL-leads-PM side (lags -1 through
-3) rather than the PM-leads-HL side, and none of those negative numbers individually
clear the noise floor either. If anything is hinted at, it is the opposite of the
informed-prediction-market story: BTC moves first, prediction-market probability adjusts
a half-day to a day later, and the joint magnitude is too small to trade against costs.

The honest read is that over this particular 30-day window, with the data resolution
available, there is **no exploitable joint signal** between this PM market and BTC perp.

## What we would need to confirm or refute

- **Tick-level Polymarket data.** Our 12h aggregation forfeits any sub-12h FOMC-window
  dynamics — exactly when the cross-venue signal should be strongest. A historical
  retrospective using the CLOB `/trades` endpoint (which is not floored at 12h) would
  reconstruct the PM probability series at trade resolution and let us test the
  hour-around-announcement window directly. The CLOB API exposes this; ingestion was
  out of scope for the 5-day build but is mechanically straightforward.
- **An actual FOMC announcement inside the sample window.** The 2026-04-21 → 2026-05-21
  window contains no scheduled FOMC meeting; the next is in June. The expected
  cross-venue propagation is concentrated around announcements, and a 30-day window
  with zero meetings is a poor experimental setup. Re-running across a 6-month sample
  that contains 3-4 meetings, with the same 12h method, would be the next thing to do.
- **A finer-grained pair.** A "Will the Fed cut rates by ≥25bp at the next meeting?"
  binary, run in the 48 hours either side of a scheduled FOMC date, would give the
  cleanest natural experiment. The event-specific market did not have 30 days of
  history at run time (the active markets resolve too fast for monthly windows).
- **A different HL asset.** BTC is dominated by non-macro flow (ETF flows, on-chain
  liquidations, exchange-specific moves). ETH funding skew, or a smaller-cap perp with
  higher beta to risk-on sentiment, would amplify any cross-venue signal that exists.
  We picked BTC because it has the deepest HL book and the cleanest hourly candles;
  the tradeoff is exactly the dilution we appear to be seeing.
- **More history.** N=60 is barely enough to detect r > 0.26. A 6-month window at the
  same granularity gives N≈360 and SE on r of ~0.05; signals in the 0.1-0.2 band would
  become measurable.

## Why an honest null is itself a useful result

The cross-venue arbitrage thesis is a beautiful story and the obvious next thing to
build after the single-venue legs land — every quant interview answer to *"what would
you do next?"* on a prediction-market project ends with some version of "pair it with
on-chain." But there is a meaningful difference between *building the detector* (an
afternoon's wiring of two existing fetchers) and *running the detector and reporting
that nothing was there at the resolution the data permits*. The first produces a
plausible-looking module; the second produces a defensible answer to the actual
question the founder cares about, which is "is there alpha here". The null finding,
with the noise band stated, the lag at which the maximum spurious correlation appears
and why it does not clear, and the specific follow-up that would change the answer,
is more useful than any number of additional infrastructure. The bar for shipping in a
small quant shop is "I looked, I found nothing exploitable at this resolution, here is
exactly what I would need to look again at the resolution where the answer lives" —
this document is built to clear that bar.
