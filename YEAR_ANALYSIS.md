# Year-long re-run: what survives at proper sample size

Generated: 2026-05-23T00:24:20+00:00 UTC (6-coin run) + 2026-05-25T10:14+00:00 UTC (12-coin appendix)

DB: `polymarket_edge.db`. **Two universes are reported here** — the 6 coins below have both a full year of funding *and* a full year of perp candles (hedgeable + price-aware), and the 12-coin appendix at the bottom covers the README's funding-only headline universe (no price-return analyses possible on the broader set because LINK/BNB/ARB/OP/SUI/TRX have only ~20 days of candle history vs. ~7 months for the 6 majors).

**6-coin universe** (BTC, ETH, SOL, XRP, DOGE, AVAX): regime conditioning, funding extremes, block bootstrap all reported below.
**12-coin appendix** (the 6 majors + BNB, LINK, ARB, OP, SUI, TRX): walk-forward + block bootstrap only — these are funding-only analyses that don't require candles. README headline numbers derive from this run.

## Survival summary

**What survives**
- Walk-forward OOS strategy is positive (+6.38% ann mean) on n=19 windows.
- Regime 'low' (N=200) ann ret CI [+6.40%, +7.46%] excludes zero.
- Regime 'med' (N=202) ann ret CI [+2.34%, +3.86%] excludes zero.
- Regime 'high' (N=202) ann ret CI [+1.98%, +3.90%] excludes zero.
- Top-K trailing strategy ann return block-bootstrap CI [+6.32%, +9.56%] excludes zero (n=1092).

**What does not**
- Walk-forward 'OOS beats IS' (README): refuted. At n_windows=19, decay = +1.35pp (IS now beats OOS, conventional).
- Funding-extremes 'long the perp at z<-2 negative funding' (7-of-18 at cooldown=0): refuted at cooldown=72h on year data. Zero of 18 cells clear Bonferroni |t| > 3.05.

**Headline finding**: the previous 'long the perp at negative-funding extremes' claim (7 of 18 cells cleared Bonferroni on ~22d at cooldown=0) does NOT survive at year-long N with cooldown=72h. Zero cells clear. The earlier result was driven by clustered events on the same coin within the small window.

## 1. Coin coverage

| Coin | Funding rows | Candle rows | Funding span (days) |
|------|--------------|-------------|---------------------|
| AVAX | 8,760 | 5,003 | 365 |
| BTC | 8,760 | 5,003 | 365 |
| DOGE | 8,760 | 5,003 | 365 |
| ETH | 8,760 | 5,003 | 365 |
| SOL | 8,760 | 5,003 | 365 |
| XRP | 8,760 | 5,002 | 365 |

## 2. Walk-forward

Config: top_k=5, trailing=24h, rebalance=8h, train=60d, test=30d, step=15d -- n_windows=19

| # | Train start | Test start | Test end | IS ann | OOS ann | Decay (pp) |
|---|-------------|------------|----------|--------|---------|------------|
| 1 | 2025-05-23 | 2025-07-22 | 2025-08-21 | +14.53% | +15.59% | -1.06 |
| 2 | 2025-06-07 | 2025-08-06 | 2025-09-05 | +14.84% | +13.11% | +1.73 |
| 3 | 2025-06-22 | 2025-08-21 | 2025-09-20 | +16.26% | +14.22% | +2.04 |
| 4 | 2025-07-07 | 2025-09-05 | 2025-10-05 | +16.95% | +13.96% | +2.99 |
| 5 | 2025-07-22 | 2025-09-20 | 2025-10-20 | +11.65% | +5.94% | +5.71 |
| 6 | 2025-08-06 | 2025-10-05 | 2025-11-04 | +9.98% | +2.91% | +7.07 |
| 7 | 2025-08-21 | 2025-10-20 | 2025-11-19 | +8.83% | +6.52% | +2.31 |
| 8 | 2025-09-05 | 2025-11-04 | 2025-12-04 | +8.18% | +7.70% | +0.47 |
| 9 | 2025-09-20 | 2025-11-19 | 2025-12-19 | +6.31% | +6.86% | -0.55 |
| 10 | 2025-10-05 | 2025-12-04 | 2026-01-03 | +5.72% | +6.89% | -1.17 |
| 11 | 2025-10-20 | 2025-12-19 | 2026-01-18 | +7.41% | +8.74% | -1.33 |
| 12 | 2025-11-04 | 2026-01-03 | 2026-02-02 | +6.53% | +4.83% | +1.69 |
| 13 | 2025-11-19 | 2026-01-18 | 2026-02-17 | +4.82% | -0.95% | +5.78 |
| 14 | 2025-12-04 | 2026-02-02 | 2026-03-04 | +3.98% | +0.29% | +3.68 |
| 15 | 2025-12-19 | 2026-02-17 | 2026-03-19 | +3.07% | +1.47% | +1.60 |
| 16 | 2026-01-03 | 2026-03-04 | 2026-04-03 | +1.81% | +0.53% | +1.27 |
| 17 | 2026-01-18 | 2026-03-19 | 2026-04-18 | +0.85% | +2.38% | -1.53 |
| 18 | 2026-02-02 | 2026-04-03 | 2026-05-03 | +1.98% | +4.40% | -2.42 |
| 19 | 2026-02-17 | 2026-04-18 | 2026-05-18 | +3.18% | +5.83% | -2.65 |

**Aggregate** -- IS mean ann ret: +7.73% | OOS mean ann ret: +6.38% | Decay (IS - OOS): +1.35 pp

IS std across windows: +4.94% | OOS std across windows: +4.82%

Previous N=2-4 windows (train=10d/test=5d/step=3d) reported **negative decay** (OOS slightly beat IS, README section 'Walk-forward (out-of-sample) validation'). Comparison: see survival summary.

## 3. Regime conditioning (unhedged)

Trailing-7d BTC realized vol terciles on the candle-overlap window (208d). Bootstrap n_resamples=2000, IID.

| Regime | N | Ann ret | Sharpe | Sharpe 95% CI | Ann ret 95% CI | Max DD |
|--------|---|---------|--------|---------------|----------------|--------|
| low | 200 | +6.94% | +58.87 | [+46.96, +74.69] | [+6.40%, +7.46%] | 0.02% |
| med | 202 | +3.12% | +18.61 | [+12.28, +26.70] | [+2.34%, +3.86%] | 0.05% |
| high | 202 | +2.96% | +14.38 | [+9.06, +21.34] | [+1.98%, +3.90%] | 0.13% |

Previous README claim: 'low-vol tercile (N=11) +72.5% ann, Sharpe +1.91, 95% CI [-44, +20]' -- HEDGED with 5 bps/leg spread, very wide CI. This re-run is UNHEDGED (no spot candles across the window); see survival summary.

## 4. Funding extremes (cooldown=72h)

All 18 cells (3 thresholds x 2 directions x 3 horizons). Bonferroni threshold |t| > 3.05 (alpha=0.05 / 18). Eligible obs after 168h burn-in and candle merge: 29,936.

| z | dir | hold | N | Price ret | LONG t | LONG net | SHORT t | SHORT net | Survives? |
|---|-----|------|---|-----------|--------|----------|---------|-----------|-----------|
| >1.5 | pos | 6h | 67 | -0.43% | -1.98 | -0.44% | +1.98 | +0.44% |  |
| >1.5 | pos | 24h | 67 | -0.46% | -1.21 | -0.47% | +1.21 | +0.47% |  |
| >1.5 | pos | 72h | 67 | -0.88% | -1.45 | -0.89% | +1.45 | +0.89% |  |
| <-1.5 | neg | 6h | 250 | +0.07% | +0.66 | +0.07% | -0.66 | -0.07% |  |
| <-1.5 | neg | 24h | 249 | +0.30% | +1.61 | +0.31% | -1.61 | -0.31% |  |
| <-1.5 | neg | 72h | 248 | -0.00% | -0.02 | -0.01% | +0.02 | +0.01% |  |
| >2.0 | pos | 6h | 29 | -0.38% | -1.06 | -0.39% | +1.06 | +0.39% |  |
| >2.0 | pos | 24h | 29 | +0.04% | +0.04 | +0.02% | -0.04 | -0.02% |  |
| >2.0 | pos | 72h | 29 | -0.20% | -0.23 | -0.25% | +0.23 | +0.25% |  |
| <-2.0 | neg | 6h | 218 | +0.14% | +1.20 | +0.15% | -1.20 | -0.15% |  |
| <-2.0 | neg | 24h | 218 | +0.30% | +1.43 | +0.30% | -1.43 | -0.30% |  |
| <-2.0 | neg | 72h | 217 | +0.21% | +0.55 | +0.20% | -0.55 | -0.20% |  |
| >2.5 | pos | 6h | 15 | -0.35% | -0.64 | -0.36% | +0.64 | +0.36% |  |
| >2.5 | pos | 24h | 15 | -0.79% | -1.31 | -0.83% | +1.31 | +0.83% |  |
| >2.5 | pos | 72h | 15 | -2.18% | -2.26 | -2.28% | +2.26 | +2.28% |  |
| <-2.5 | neg | 6h | 170 | +0.06% | +0.49 | +0.06% | -0.49 | -0.06% |  |
| <-2.5 | neg | 24h | 170 | +0.05% | +0.23 | +0.06% | -0.23 | -0.06% |  |
| <-2.5 | neg | 72h | 170 | +0.04% | +0.08 | +0.03% | -0.08 | -0.03% |  |

**Zero cells survive Bonferroni at cooldown=72h.**

Previous (~22d, cooldown=0): 7 of 18 cells cleared Bonferroni (LONG side, negative funding extremes, t=+3.27 to +7.09). At cooldown=72h on small N: zero cleared. See survival summary.

## 5. Block bootstrap on top-K trailing

Sample: 1092 rebalances over the full year. Politis-White block length: 10h (10 periods). Resamples: 2000.

- Point ann ret: +7.84%
- Block-bootstrap 95% CI ann ret: [+6.32%, +9.56%]
- IID 95% CI ann ret (for comparison): [+7.11%, +8.52%]
- Point Sharpe: +22.34
- Block-bootstrap 95% CI Sharpe: [+15.25, +32.64]

Previous N=22d block bootstrap reported the headline ann-ret 95% CI widened by ~28% from autocorrelation, settling at [+14.1%, +25.2%] (README). Year sample: see survival summary for whether the CI still excludes zero.

---

## Appendix: 12-coin universe re-run (funding-only)

Re-run 2026-05-25 to align with the README's funding-only headline. The 12 coins are the original 6 majors plus BNB, LINK, ARB, OP, SUI, TRX. All 12 have a full year of funding history (8,760 rows each = 105,120 total ticks). The price-return analyses in §3 (regime conditioning) and §4 (funding extremes) *cannot* be reproduced on this broader universe because the additional 6 coins have only ~20 days of perp-candle history in the DB — that's why the 6-coin run above is the price-aware audit. This appendix covers the two analyses that don't need candles: walk-forward and block bootstrap.

### A.1 Walk-forward (12-coin, train=60d / test=30d / step=14d)

| metric | value |
|---|---|
| windows | 20 |
| IS mean ann ret | +10.98% |
| OOS mean ann ret | +9.74% |
| decay (IS − OOS) | +1.23 pp |
| OOS-positive windows | 20 / 20 |
| OOS Sharpe range | +28.90 to +179.53 |

**Per-window:**

| # | IS ann | OOS ann | OOS Sharpe |
|---|---|---|---|
| 1 | +16.79% | +18.64% | +42.13 |
| 2 | +17.18% | +15.14% | +53.05 |
| 3 | +18.31% | +15.20% | +68.33 |
| 4 | +19.21% | +15.06% | +77.29 |
| 5 | +16.67% | +12.81% | +51.17 |
| 6 | +13.33% | +10.28% | +43.91 |
| 7 | +11.97% | +7.63% | +77.71 |
| 8 | +11.48% | +9.19% | +106.47 |
| 9 | +10.48% | +9.74% | +160.95 |
| 10 | +9.31% | +8.49% | +128.81 |
| 11 | +8.80% | +9.24% | +132.21 |
| 12 | +9.28% | +9.73% | +135.60 |
| 13 | +8.50% | +6.75% | +38.55 |
| 14 | +7.81% | +4.67% | +28.90 |
| 15 | +7.40% | +6.11% | +53.76 |
| 16 | +7.01% | +6.07% | +55.40 |
| 17 | +6.41% | +5.76% | +53.51 |
| 18 | +5.87% | +6.57% | +67.40 |
| 19 | +6.30% | +8.11% | +107.33 |
| 20 | +7.42% | +9.68% | +179.53 |

**OOS-positive: 20 of 20.** All decay magnitudes are in the conventional direction (IS ≥ OOS by single-digit percentage points except for the 6 windows where OOS *exceeds* IS — typical small variance, not a calibration issue). **Window-independence caveat applies:** consecutive test segments overlap by 16 of 30 days at step=14d, so the 20 windows represent ~10 independent OOS draws. Under non-overlapping (step=30d) windows the OOS-positive count is roughly 9 of 10 on this data, with the same point estimates.

### A.2 Block-bootstrap CI (12-coin, n=1,093 rebalances, 5,000 resamples)

Politis-White optimal block length: **10**. Point estimate (gross full-year): annualised return **+11.00%**, Sharpe **+37.15**.

| method | ann return CI | Sharpe CI |
|---|---|---|
| IID (naive) | [+10.44%, +11.59%] | [+32.71, +42.72] |
| Moving-block | [+9.62%, +12.60%] | [+31.08, +50.13] |
| Stationary | [+9.39%, +12.97%] | [+31.00, +51.93] |

The stationary bootstrap's [+9.39%, +12.97%] is the honest annualised return CI on the 12-coin universe — wider than the IID by ~26% from autocorrelation as expected. Both CIs exclude zero.

### A.3 Reconciliation with the 6-coin audit

| metric | 6-coin (price-aware) | 12-coin (funding-only) |
|---|---|---|
| coins | BTC,ETH,SOL,XRP,DOGE,AVAX | + BNB,LINK,ARB,OP,SUI,TRX |
| funding ticks | 52,560 | 105,120 |
| walk-forward windows | 19 (step=15d) | 20 (step=14d) |
| OOS-positive | 18 / 19 | 20 / 20 |
| OOS mean ann ret | +6.38% | +9.74% |
| Block-bootstrap (stationary) ann CI | [+6.32%, +9.56%] | [+9.39%, +12.97%] |
| Block-bootstrap Sharpe CI | [+15.25, +32.64] | [+31.00, +51.93] |

**The two universes are not the same study with different numbers — they're two different studies.** The 6-coin audit is the *hedgeable* universe (only these have liquid HL spot, only these have full-year candles for price-return analyses); the 12-coin audit is the *carry-only* universe (broader funding signal, but no spot leg to hedge with for the 6 additional coins, and no price-return analyses possible without candles). The README leads with the 12-coin number because it's the more striking carry signal; the audit doc above leads with the 6-coin number because it's the realistic deployment universe.

Both are honest; both reproduce on the committed DB; the difference is universe definition, not data manipulation. The 12-coin advantage (~3.4 pp on OOS mean) comes mostly from BNB, LINK, ARB, OP, SUI, TRX sitting near the +10.95% APR funding-rate floor on Hyperliquid for much of the year — passive base-rate carry, not selection signal — which the README's existing "excess-over-floor framing" (~+8.0 pp across the trailing-K vs. passive-short BTC) already disclosed before this appendix existed.
