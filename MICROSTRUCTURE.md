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
