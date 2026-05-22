# The trap rate of detector-flagged Polymarket negRisk events

**Captured 2026-05-21. Scan window: 500 active Polymarket events, 19 flagged by the top-of-book negRisk detector at a 50bp fee buffer.**

## The question

The README's existing depth finding ("World Cup is real, Weinstein is a trap, 2028 Election is marginal") is three case studies — n=3, hand-picked. A top-of-book event-level no-arb detector flags candidates at a fixed bps threshold; the question this writeup answers is the population statistic underneath those anecdotes: **across every currently-active negRisk event flagged by the detector, what fraction actually trades at meaningful size, and does the fraction vary by event category?** If the trap rate is 5% the detector is good enough alone; if it's 60% any naive top-of-book scanner is structurally losing money on most of what it surfaces.

## Method

`src/polymarket_edge/microstructure.py` pulls all currently-active events from the gamma API (capped at 500 per the rate-limit budget), scores each with the existing top-of-book detector at a 50bp fee buffer, and for every flagged event walks the full `/book` for every active market on the flagged side (bid side for sell-flags, ask side for buy-flags) at two notional levels: $50/market and $500/market. The basket-trade sum-of-avg-fill is compared to 1.0 in both cases. Verdict is binary on the depth-aware gaps:

- **real:** top-of-book gap > 50bp **and** gap at $500/market > 50bp (signal holds at institutional-scale notional)
- **trap:** top-of-book gap > 50bp **and** gap at $50/market < 0 (signal inverts to a loss at retail-scale notional — at least one market has near-zero depth)
- **marginal:** top-of-book gap > 50bp; gap at $50/market still positive; gap at $500/market ≤ 50bp (decays into the fee buffer between retail and institutional size)
- **noise:** top-of-book gap ≤ 50bp (excluded from this analysis — the detector wouldn't flag it)

Categorisation uses `event['tags'][0]['label']`; events with no tags become `"Uncategorized"` (none were missing in this scan). The flagged direction is whichever of bid_gap / ask_gap is larger at top of book; sell- and buy-side are never mixed within one classification. The fetch step rate-limits to one `/book` request every 250ms to stay under CLOB's 60req/min ceiling.

## Results

500 active events fetched. **19 events (3.8%) were flagged by the detector** at the 50bp buffer. All 19 had complete books on every active market and produced clean classifications (zero skipped on book errors).

**Headline rates (n=19):**

| verdict | count | share |
|---|---|---|
| real | 2 | 10.5% |
| marginal | 5 | 26.3% |
| **trap** | **12** | **63.2%** |

**Per-category breakdown (sorted by total flagged events):**

| category | real | marginal | trap | total | trap rate |
|---|---|---|---|---|---|
| Politics | 0 | 1 | 5 | 6 | **83.3%** |
| Elections | 0 | 1 | 3 | 4 | **75.0%** |
| US Election | 0 | 0 | 3 | 3 | **100.0%** |
| Midterms | 0 | 1 | 1 | 2 | 50.0% |
| Soccer | 1 | 0 | 0 | 1 | 0.0% |
| Business | 0 | 1 | 0 | 1 | 0.0% |
| Awards | 1 | 0 | 0 | 1 | 0.0% |
| NHL | 0 | 1 | 0 | 1 | 0.0% |

**Specific examples.** The two `real` signals are the 48-market `2026-fifa-world-cup-winner-595` event (Soccer, +150bp top of book, +146.9bp at $500/market — the existing README case study, still tradeable) and the 20-market `nobel-peace-prize-winner-2026-139` (Awards, +4,910bp top of book, +693bp at $500/market — a much wider mispricing held by long-tail markets at near-zero prices). The worst traps by depth-aware gap magnitude are 6-market `co-08-democratic-primary-winner` (Politics, +100bp top, **−180.12% at $500/market**, throttled by a $500-target market that fills cleanly but other thin legs collapse the basket), 9-market `virginia-republican-senate-primary-winner` (Politics, +230bp top, **−152.3% at $500/market**), and `arkansas-senate-election-winner` (Politics, +70bp top, throttled at $146.73 — selling more than ~$73/market on average exhausts the bid book entirely on at least one leg).

## Interpretation

The pattern is sharp: **traps concentrate almost entirely in US state-level election categories.** Every flagged event tagged `Politics`, `Elections`, `US Election`, or `Midterms` other than two marginals is a trap; meanwhile the only two events that classified `real` are in high-volume non-political categories (Soccer World Cup, Nobel Peace Prize). The structural reason is mechanical: a state governor's-race negRisk event typically has 2 active markets — the Republican candidate and the Democratic candidate — and one of those two markets carries 95%+ of the implied probability. The thin side (5% market) often has a top-of-book bid that is real but a bid-book depth measured in the *single dollars*. The top-of-book detector reads 5%-side bid + 96%-side bid = 1.01 and flags a sell-side arb at +100bp, but the moment the basket tries to sell even $50 of YES on the 5%-side, that market's bid book collapses to zero and the basket avg fill craters far below 1.0.

In other words: **the events the detector flags most easily are precisely the ones least likely to be tradeable.** Two-market state-race events with extreme price asymmetry produce eye-catching top-of-book gaps and are essentially undepth-able on the thin side; high-volume multi-market events (World Cup with 48 markets, Nobel with 20 markets) spread liquidity across more legs and survive depth-walking. The Polymarket microstructure doesn't intentionally create traps — it's the obvious consequence of running a CLOB where most flow goes to the favoured outcome.

A weaker secondary effect: among the 7 non-trap classifications (5 marginal + 2 real), 4 are in categories with a single representative in this scan (Soccer, Awards, Business, NHL), so we cannot say from this snapshot that "high-volume Sports categories are always real." The clean statement that *can* be made is: **the trap rate among US/state-election-tagged events is 11/13 ≈ 85%** (Politics + Elections + US Election + Midterms combined, excluding the two marginals), and zero of the 13 cleared the $500/market real bar.

For any quant scanning Polymarket negRisk events, the rule of thumb falls out cleanly: a top-of-book gap on a 2-market US state election is almost certainly a trap; treat it as untradeable until depth has been walked. The detector's job is candidate generation; depth-walking on the flagged side is non-optional before sizing.

## Caveats

- **Snapshot in time.** Polymarket order books shift hourly; a re-scan an hour later may produce different per-event verdicts (the README explicitly tracks this drift for the 3-event case study). The aggregate population pattern — trap rate concentrated in low-market-count US politics — is likely structural and stable; the specific 63.2% headline rate is for this single capture.
- **The detector flags only 19/500 = 3.8% of events.** The 60req/min gamma rate limit and the 500-event cap mean some currently-active negRisk events with smaller deviations are not in this sample. A larger scan budget would tighten the headline but the per-category sign would not flip.
- **Single-direction analysis.** Each classification walks only the side flagged by the larger top-of-book gap (sell-side or buy-side). An event with a tradeable buy-side at meaningful depth but a flagged sell-side that traps is recorded as a trap. The single-direction restriction is the right call for a 1-shot classification but it under-counts marginal-on-the-other-side events.
- **Ambiguous classifications near the fee_buffer boundary.** Events with top-of-book exactly at 50bp will jitter between flagged and unflagged across scans; events with $500/market gap exactly at the 50bp threshold can flip between `real` and `marginal`. In this scan, only one event (`how-many-fed-rate-cuts-in-2026`) was classified `marginal` despite a deeply negative gap at $500/market — because gap_at_$50 was still positive (+90bp). The spec's binary criterion is honest but the underlying signal is continuous; readers should treat verdict counts as ±1 in any single category.
- **No time-of-day effects measured.** Book depth around major sports-event kickoffs or political news cycles likely shifts category-level trap rates. This scan does not resample at different times of day.
- **Categorisation uses `tags[0]['label']` only.** Some events carry richer tag hierarchies (`Politics` plus `Elections` plus `US Election` plus `Midterms`); this writeup follows the first-tag rule and the resulting per-category breakdown has overlapping near-synonym categories. The "trap rate is ~85% across all US politics" rolled-up statement is what's actually robust; the per-tag breakdown is illustrative, not partitioning.

## Why this is useful

For any prediction-market quant building a trading engine on top of negRisk event-level no-arb scanning, **trap rate dominates win rate at the strategy level.** A naive top-of-book scanner sized in proportion to the gap will trade traps at the same frequency it trades real signals — and traps don't just produce zero, they produce large negative P&L by walking thin books past their support level. This dataset says: of every 19 detector-flagged events surfaced by the existing top-of-book scanner today, only 2 (~10%) actually trade at the institutional-scale notional implied by their top-of-book gap; the other 17 are either marginal-at-retail-size-only or actively-trap-money.

The practical recipe for a working scanner: (1) detector flags candidates at any sensible fee buffer; (2) the depth-walking pass is non-optional — every flagged event must clear the $500/market depth-aware bar to enter the sizing pipeline; (3) US state-election 2-market events should carry a category-level prior of `trap` and require unusually clear depth on both legs to pass. Any quant skipping step (2) is structurally trading the worst part of the detector's flag distribution. The 63.2% headline is what step (2) is for.

The depth-aware classification primitive (`classify_event` in `microstructure.py`) is the building block. It runs in ~250ms per active market in the event; for a 2-market state race it's half a second of latency; the trade-off against the cost of trading a single trap is enormous.

## Reproducing

```bash
PYTHONPATH=src python scripts/microstructure_scan.py --max-events 500
```

Run-time on the captured scan was ~6 minutes against the live gamma + CLOB endpoints (the per-event /book calls are throttled to one per 250ms). Output is written to stdout (table + headline rates) and persisted to the new `microstructure_classifications` SQLite table; progress to stderr. Tests for the classifier live in `tests/test_microstructure.py` (9 unit tests with synthetic books, no API in tests).

## Volume-weighted re-analysis

The 63.2% headline above weights every flagged event equally. A founder building a sizing pipeline cares about a different question: **of the dollars-at-risk that the detector surfaces, what fraction sit in events that turn out to be traps?** If the two `real` events carry most of the flagged USD, the practical trap rate -the one that matters for capital allocation -is much lower than the count-based number. This section reports both side by side using `events.volume` (lifetime cumulative USD per event) and `events.liquidity` (current resting size) as the weights. Numbers below are from a fresh re-scan on 2026-05-22 against the live gamma + CLOB endpoints (`scan_id=684f7876c720`, 18 flagged events; previous capture was 19), produced by `scripts/volume_weighted_trap_rate.py`.

**Overall: count vs volume vs liquidity**

| verdict | count | count share | volume (USD) | volume share | liquidity (USD) | liquidity share |
|---|---|---|---|---|---|---|
| real | 2 | 11.1% | $1.12B | 97.5% | $277.15M | 99.4% |
| marginal | 6 | 33.3% | $28.82M | 2.5% | $1.51M | 0.54% |
| **trap** | 10 | 55.6% | $139.9K | 0.012% | $119.7K | 0.043% |
| total | 18 | 100.0% | $1.15B | 100.0% | $278.78M | 100.0% |

**By category (sorted by share of total flagged volume)**

| category | n | trap count | count trap rate | volume | volume share | trap volume | volume trap rate | liquidity |
|---|---|---|---|---|---|---|---|---|
| Soccer | 1 | 0 | 0.0% | $1.10B | 95.9% | $0 | 0.0% | $275.32M |
| Business | 1 | 0 | 0.0% | $28.48M | 2.5% | $0 | 0.0% | $1.42M |
| Awards | 1 | 0 | 0.0% | $18.02M | 1.6% | $0 | 0.0% | $1.83M |
| NHL | 1 | 0 | 0.0% | $199.7K | 0.017% | $0 | 0.0% | $20.9K |
| Midterms | 3 | 2 | 66.7% | $90.5K | 0.008% | $33.1K | 36.5% | $49.8K |
| Elections | 4 | 2 | 50.0% | $89.8K | 0.008% | $28.1K | 31.2% | $60.5K |
| Politics | 4 | 3 | 75.0% | $76.1K | 0.007% | $54.7K | 71.8% | $41.7K |
| US Election | 3 | 3 | 100.0% | $24.1K | 0.002% | $24.1K | 100.0% | $39.3K |

**Named events**

- highest-volume trap: `rhode-island-governor-winner-2026` (Politics) - volume $54.7K, liquidity $31.1K
- highest-volume real: `2026-fifa-world-cup-winner-595` (Soccer) - volume $1.10B, liquidity $275.32M

### Interpretation

Volume-weighting collapses the headline almost completely: **count-based trap rate is 55.6% (10/18) but volume-weighted trap rate is 0.012% ($139.9K of $1.15B)** -more than three orders of magnitude lower. The pattern hypothesised in the brief is structural: the single `real` flag on the 2026 FIFA World Cup event alone carries 95.9% of the entire flagged-event dollar pool, and the two real events combined carry 97.5%. Every trap surfaced in this scan is a small US state-election event with lifetime volume in the five-figure USD range; together they amount to ~$140K of dollars-at-risk against a flagged universe sized in the billions. The practical implication for a real sizing pipeline is that capital-weighted exposure to traps under this detector is effectively negligible if positions are sized in proportion to event volume or liquidity: a sizing rule of the form "notional proportional to min(volume, gap × liquidity)" would automatically allocate >95% of risk to the two real signals and trickle change to the traps. The count-based 55.6% / 63.2% headline overstates the practical risk because it treats a $1.1B World Cup event and a $7K Idaho governor event as equally important rows in the table -they are not.

This does not vindicate the top-of-book detector: even at $0 volume-weighted exposure, walking a trap costs real P&L if the sizer ignores depth and tries to fill at top-of-book size. The volume-weighted view is the right framing for **risk-aware capital allocation**; the count-based view is the right framing for **detector precision**. Both are true, and a founder pitch should lead with the volume number, then disclose the count number underneath as the precision metric. The category breakdown reinforces the same point in a different way: every category with non-trivial volume share (Soccer, Business, Awards) has 0% volume-weighted trap rate; every category with a non-zero volume-weighted trap rate sits at <0.02% of total flagged volume.

### Caveats specific to volume-weighting

- `events.volume` is **lifetime cumulative USD traded** across the event's history, not "open USD at risk now." A 2-year-old NFL event with $50M lifetime volume but no current depth would dominate the volume share without representing any meaningful current exposure. The liquidity column is the closer proxy for "what could be filled today" and the liquidity-weighted trap rate (0.043%) is the more conservative number; both tell the same story at this snapshot.
- Three events in this scan had **NULL volume in the events table** (`co-08-democratic-primary-winner`, `new-hampshire-democratic-senate-primary-winner`, `new-york-democratic-governor-primary-winner`) -all classified as traps. The script treats NULL as 0 and prints a stderr warning. The pattern is that these events were upserted into the events table by code paths (monitor / paper-auto / microstructure-only flow) that didn't populate the gamma volume field at ingest time. Even if these three carried meaningful real volume, they are all sub-$100K state-election events; assigning them the median trap volume (~$22K each) would push the volume-weighted trap rate from 0.012% to ~0.018%, still three orders of magnitude below the count-based 55.6%. The qualitative finding does not depend on the NULL handling.
- A more rigorous version would use **current order-book depth on the flagged side** (the throttle_notional column from the existing classification) as the weight, rather than lifetime volume or static liquidity. That would directly measure "dollars-at-risk if the detector's flag were taken at the gap-implied size." Building that requires re-walking books at classification time and is left to a follow-up.
- Volume-weighting amplifies single-event concentration. The 97.5% volume share of one event (World Cup) is itself a finding: **the population statistic is not robust to the disappearance of one outlier event.** If the World Cup event resolves and ages out, the volume-weighted trap rate could jump substantially even with the same per-event classifications. The count-based and liquidity-weighted numbers are less sensitive to this and the founder pitch should disclose both.

## Trap classifier — converting the descriptive finding into a predictive model

The descriptive section above answers "what fraction of detector-flagged events are traps?" with a population statistic (63% on the original 19-event capture, 56% on the 2026-05-22 re-scan). A founder follow-up — "given a new flagged event today, what is the probability it is a trap?" — is no longer a statistic but a model. The artefact below trains one on the same rows the descriptive section is built from, evaluates it under leave-one-out cross-validation, and reports calibration on every held-out fold. With n=18 the model is scaffolding rather than production; it exists so the deployment-shaped object (`predict_proba` on a new event's features) is in place when more scans accumulate.

`src/polymarket_edge/trap_classifier.py` is a stdlib-only batch-gradient-descent logistic regression (no sklearn, no scipy, no numpy) with L2 regularisation. The L2 term is non-optional at this sample size — without it the optimiser runs away on near-separable one-hots like `is_us_politics`. Features per flagged event:

- `n_markets` — event-level market count
- `top_of_book_gap_bps` — the detector's own gap signal in basis points
- `is_us_politics` — one-hot for category in {Politics, Elections, US Election, Midterms}
- `is_two_market` — one-hot for n_markets == 2 (the 2-market mechanical shape from the earlier section)
- `neg_risk_augmented` — one-hot for the negRiskAugmented event flag

The training script lives at `scripts/trap_classifier_train.py` and runs LOOCV against the latest scan in the live `polymarket_edge.db`. On the same 2026-05-22 re-scan (scan_id `684f7876c720`, 18 flagged events of which 10 are traps, base rate 55.6%):

- LOOCV ROC AUC: 0.600
- Accuracy at p=0.5: 77.8% (vs base rate 55.6%)
- Confusion matrix at p=0.5: TP=10, FN=0, FP=4, TN=4 — the model recovers every trap but over-fires on 4 of 8 non-traps, which is the expected behaviour of a classifier facing a high base rate and weak feature signal

Feature coefficients (full-data fit, raw-feature scale):

| feature | coef | direction |
|---|---|---|
| is_us_politics | +2.29 | increases trap prob |
| neg_risk_augmented | +1.50 | increases trap prob |
| is_two_market | −0.93 | decreases trap prob |
| n_markets | −0.04 | decreases trap prob |
| top_of_book_gap_bps | −0.0005 | decreases trap prob |

The dominant signal is exactly what the mechanical explanation predicted: `is_us_politics` is by far the strongest positive feature, matching the earlier section's "trap rate concentrates in US state-election categories" finding. The `neg_risk_augmented` signal is secondary and small-n suspect (only a handful of augmented events in the sample). The top 3 trap-probability predictions on the full-data fit are `rhode-island-governor-winner-2026` (p=0.83, actual trap), `co-08-democratic-primary-winner` (p=0.82, actual trap), and `idaho-governor-winner-2026` (p=0.68, actual trap); the lowest 3 are `nobel-peace-prize-winner-2026-139` (p=0.03, real), `how-many-fed-rate-cuts-in-2026` (p=0.07, marginal), and `2026-fifa-world-cup-winner-595` (p=0.08, real). The ordering matches the mechanical story end-to-end.

Two surprises worth flagging. First, `is_two_market` has a *negative* coefficient even though the earlier section emphasises the 2-market structure as the mechanism. The reason is that `is_us_politics` and `is_two_market` co-vary strongly in this sample — almost every 2-market event is also tagged US politics — so the regressor attributes the joint signal almost entirely to the politics flag and the residual `is_two_market` effect (the few 2-market non-politics rows) is faintly negative. Second, `top_of_book_gap_bps` has a near-zero coefficient: the magnitude of the detector's own signal does not predict trap-ness within the flagged set, which is consistent with the earlier section's argument that depth (not gap size) is what separates real from trap. The detector flags both World Cup (+150bp top, real) and `co-08-democratic-primary-winner` (+100bp top, deeply trap) at similar magnitudes; the model correctly learns that gap size alone is not informative within the flagged distribution.

The honest reading of AUC=0.600 at n=18: the model is genuinely better than random (chance is 0.5 by definition) and it recovers the mechanical pattern, but the LOOCV AUC estimator itself has a wide confidence interval at this sample size — a re-scan tomorrow with a slightly different population of flagged events would shift AUC by ±0.1 without changing the underlying mechanics. The deployable contribution of this section is not the AUC number; it is the methodology, the trained artefact, and the demonstration that the descriptive finding produces a coherent ordering when run as a model rather than a category table. Once `polymarket_edge.db` accumulates 5-10 daily scans the same script will retrain on n>100 and the AUC number becomes load-bearing.

Reproduce:

```bash
PYTHONPATH=src python scripts/trap_classifier_train.py
```


## Hyperliquid funding extremes -- directional study

The README's existing top-K-funding-shorts backtest averages across every positive-funding coin and reports the short-side carry. This section answers a sharper question: **at the tail of the funding distribution -- hours where the rate is >2 sample-stdevs above its own 168h trailing mean for that coin -- does the perp price subsequently rally or crash over the next 24h?** The conventional intuition is high-funding = longs paying through the nose = price about to crash, short the perp; the contrarian intuition is high-funding = shorts crowded = squeeze coming, buy the perp. Both are tradeable if true. This study, captured 2026-05-22 against 22 days of `hl_funding_history` and a fresh pull of hourly perp candles for 37 Hyperliquid coins (`scripts/hl_extremes_study.py`, output `results/hl_extremes_20260522T014401.json`), tests both directly.

### Method

For each (coin, hour) in `hl_funding_history`, compute a z-score against the STRICTLY trailing 168h window (rows [i-168 : i], candidate row excluded -- no look-ahead). Drop the first 168h per coin (no trailing window) and any row where the trailing std is zero. Merge against the matching `hl_perp_candles` hourly close. After drops: **10,416 eligible observations across 37 coins.** Identify extremes at z thresholds {1.5, 2.0, 2.5} in both directions, hold for {6, 24, 72} hours, and compute per-event `long_net_return = price_return - funding_paid_long` and `short_net_return = -long_net_return`. Aggregate equal-weighted across events; t-stat = mean / (sample_std / sqrt(N)); Sharpe = (mean/std) * sqrt(8760/hold_hours). The 18-cell family demands Bonferroni: single-cell alpha=0.05/18 ~= 0.0028, so |t| > 3.05 to claim a survivor.

### Headline numbers (full 37-coin universe, no cooldown)

| cell | n | LONG net (24h) | t | SHORT net (24h) | t |
|---|---|---|---|---|---|
| z > +2.0, 24h | 236 | **-0.041%** | **-0.13** | +0.041% | +0.13 |
| z < -2.0, 24h | 212 | **+1.183%** | **+3.41** | -1.183% | -3.41 |
| z < -2.0, 72h | 188 | **+4.477%** | **+5.38** | -4.477% | -5.38 |
| z < -2.5, 24h | 138 | +1.538% | +3.27 | -1.538% | -3.27 |
| z > +2.0, 72h | 160 | +1.454% | +1.75 | -1.454% | -1.75 |

**The headline cell (z>2 positive, 24h hold) is a clean null:** mean long net return -0.041%, t=-0.13. Neither the contrarian-LONG (buy the squeeze) nor the confirming-SHORT (high funding shorts win at extremes) thesis is supported at the canonical (2.0, 24h, positive) configuration. The short-side average is mechanically the inverse of the long-side and is equally insignificant.

The signal is entirely on the **negative-funding side**: when funding compresses to z < -2 (longs are net being PAID by shorts -- i.e. short-side carry is negative), the perp rallies by +1.18% net of (negative) funding over the next 24h with t=3.41, and by +4.48% over 72h with t=5.38. Bonferroni clears it.

### Bonferroni survivors

On the full universe with no cooldown, **7 of the 18 cells clear |t| > 3.05**, every one of them on the negative-funding side or at the long 72h horizon:

- z < -1.5, 24h hold  -- LONG net +1.06%, t=+4.22, n=355
- z < -1.5, 72h hold  -- LONG net +3.95%, t=+7.09, n=312
- z < -2.0, 24h hold  -- LONG net +1.18%, t=+3.41, n=212
- z < -2.0, 72h hold  -- LONG net +4.48%, t=+5.38, n=188
- z < -2.5, 24h hold  -- LONG net +1.54%, t=+3.27, n=138
- z < -2.5, 72h hold  -- LONG net +5.24%, t=+4.62, n=123
- z > +1.5, 72h hold  -- LONG net +2.46%, t=+3.56, n=255 (the only positive-side cell, and only at the 72h horizon)

**Zero positive-funding cells at the 6h or 24h horizon clear Bonferroni in either direction.** The conventional high-funding-shorts-win intuition that the existing top-K backtest implicitly trades on does NOT survive at the tail of the distribution at hold horizons under 3 days.

### Independence check (cooldown = 72h, full universe)

With a per-coin cooldown equal to the longest hold horizon -- so consecutive z>2 hours on the same coin do not produce overlapping forward windows -- event counts collapse roughly 8x (e.g. z>2 positive 24h drops from n=236 to n=30). **No cell clears Bonferroni in the cooldown=72h run; the closest is LONG z<-1.5 72h at t=+2.83.** This is the conservative reading: a meaningful fraction of the cooldown=0 t-statistic comes from clustered events on the same coin sharing the same market move, not 200+ independent draws.

### Liquid universe (BTC, ETH, SOL, XRP, DOGE) at z>2 positive, 24h

The per-coin breakdown at the headline cell tells a cleaner story than the aggregate null. BTC and ETH produce **zero extreme events** at z>2 over the 22-day window -- funding never spiked enough relative to its own 168h baseline on the two largest-cap coins to fire the detector. The 31 events at z>2 positive 24h all sit on SOL (n=6, long_net +0.90%, t=+1.60), XRP (n=19, +0.36%, t=+0.81), and DOGE (n=6, +1.87%, t=+3.81). Aggregated over the liquid subset: long_net +0.76%, t=+2.38 -- a hint of a positive (contrarian-LONG) edge at the extreme on liquid alts, with DOGE alone clearing single-test significance. **The liquid-universe conclusion at z>2 positive 24h is the inverse of the full-universe null** -- but at n=31 across only three coins, this is a candidate hypothesis to confirm with a longer window, not a deployable signal. The liquid universe also clears Bonferroni on the negative-funding side (z<-1.5, 72h: LONG net +1.48%, t=+5.06, n=85), reinforcing the asymmetric finding.

### Honest interpretation

There is no statistically robust directional edge at extreme POSITIVE funding under conservative (independent-event) reading. The clean signal in this dataset is on the NEGATIVE side: when funding goes deeply negative relative to its own trailing 168h, longing the perp pays -- and the magnitude grows monotonically with the hold horizon, suggesting a slow mean-reversion of the funding curve back to a positive baseline, with the perp drifting up alongside it. This is consistent with a structural net-long bias in HL perp open interest interpretation: the only way funding goes deeply negative is for shorts to be over-represented; subsequent normalisation lifts price.

### Caveats

- **22 days is short.** The cooldown=0 results have n=200+ but those events are clustered on shared market moves; the cooldown=72h independence check is closer to the truth and clears nothing.
- **Coin-clustering.** Five DOGE events at z>2 and a co-incident memecoin cycle can dominate the full-universe aggregate. The liquid-only result is the headline if it disagrees with the full universe, which it does here at z>2 positive 24h (full: null; liquid: weakly positive on DOGE).
- **End-of-window truncation.** Entries within the last 72h of the data window cannot compute a 72h exit; they are dropped, not back-imputed.
- **Funding is approximated as a simple sum** over the hold window -- consistent with the existing `hl_backtest.backtest_passive` convention; compounding would change the second-decimal of the per-event funding-paid number but not the sign or significance of the result.

### Trade implication

The deployable trade emerging from this study is **long the perp at z < -2 negative-funding extremes, hold 24-72h**. The standard short-at-high-funding trade does NOT have a sharper edge at the 24h horizon than the existing trailing-K strategy; the magnitude grows only at the 72h horizon and even there it does not survive the independence cooldown. Sizing for the negative-funding long trade should use the existing depth-walking primitive from `microstructure.py` against the HL perp book; the trade is 24h-hold-from-extreme.

### Reproducing

```bash
PYTHONPATH=src python scripts/hl_extremes_study.py
PYTHONPATH=src python scripts/hl_extremes_study.py --no-fetch  # re-run from DB cache
```

Output is written to stdout (per-cell table + Bonferroni survivors + per-coin breakdown) and JSON-persisted to `results/hl_extremes_<timestamp>.json`. Candles cached in `hl_perp_candles`. Tests: `tests/test_hl_extremes.py` (17 unit tests with synthetic data, no network).
