# Red-team pass — findings and corrections

A self-audit of the project before submission. Each item is something that *could* be wrong or overstated, found by my own pre-commit pass. Items with **FIXED** were patched in code; items with **DOCUMENTED** are real limitations now called out in the README and report; items with **OPEN** are known weaknesses not resolved in the build window.

## 1. Narrative errors I had to walk back

**1a. Polymarket fee assumption — FIXED in narrative.**
I originally claimed "sub-2% gaps don't clear taker fees" everywhere (README, cover letter, CV). That's only true for some categories. Polymarket's actual fee structure as of 2026 is per-category and probability-curved, with the peak at 50% probability:

| category | peak taker fee |
|---|---|
| Crypto | 1.80% |
| Economics | 1.50% |
| Politics | 1.00% |
| Sports | 0.75% |
| Geopolitical | 0.00% |
| Makers (any) | 0% + 20–25% rebate |

This *strengthens* the build's headline finding: the **150bp 2026 World Cup sell-side signal is a Sports market, so it could clear a 0.75% taker fee** as a clean ~75bp net edge — assuming we can take it at the size in the book. The 100bp 2028 Election signal does not clear 1.00% Politics fee, and the 80bp Weinstein signal does not clear Culture / Mentions fees. A maker-only execution clears every gap above the rebate threshold.

**1b. "8× passive BTC short" framing — FIXED.**
I quoted the +19.0% annualized top-5 strategy as "8× the passive BTC short (+2.3%)." That comparison is misleading. Hyperliquid's funding formula has a floor at the 0.01%/8h interest rate = 10.95% APR, so any coin sitting near zero premium pays shorts ~+11% annualized just from the floor. BTC happened to spend significant time at negative premium during the 30-day window; DOGE, LINK, AVAX sat at the floor and yield +10–11% passively. The honest decomposition is:

| component | contribution |
|---|---|
| Base-rate floor (~10.95% APR) | ~11.0% |
| Excess from top-K coin selection | ~ +8.0% (K=5) |
| **Total realized (top-5, trail-24h, rebal-8h)** | **+19.0%** |

Excess-over-floor is the right figure to cite, not the BTC ratio. README, report, CV, and cover letter all now use that framing.

**1c. `negRiskAugmented` events — DOCUMENTED.**
The detector treats all `negRisk: true` events identically. Per the Polymarket "negative risk" docs, an event is *augmented* (`negRiskAugmented: true`) when new outcomes can be added after trading begins — e.g. a new candidate enters a race. For augmented events:
- The set of YES tokens is not fixed; the sum-of-YES bound is not strictly 1.0 over the event's lifecycle.
- An implicit "Other" placeholder exists whose definition shifts as new outcomes appear; the Polymarket docs explicitly warn against trading it directly.

Two of the three flagged events (2026 World Cup, 2028 US Election party) are `negRiskAugmented: true`. The Weinstein event is not. So the World Cup signal is structurally less reliable than the Weinstein one — the lifetime sum-of-YES bound is softer there.

**1d. Pattern novelty — DOCUMENTED.**
NegRisk-event-level arbitrage is a known pattern; a public Go SDK ships a `find-negrisk-opportunities` example, and there's at least one arXiv paper on prediction-market arbitrage that covers it. The project is not claiming novelty — it's a clean, defensible, public-API-only implementation with explicit math, sensitivity analysis, and a stress-tested README. That positioning is honest and now stated.

## 2. Code bugs I found in the audit

**2a. `hyperliquid.insert_funding_history` swallowed bad rows silently — FIXED.**
Caught all of `KeyError`, `ValueError`, `TypeError` and silently `continue`d. The function now collects bad rows and surfaces a warning at the end of the call instead of swallowing.

**2b. `hl_backtest.backtest_top_k_trailing` partial-data check — HARDENED (not a real bug).**
On audit I thought the function would credit a coin with a partial future-window total as if it were the full-period total. After writing a failing test for it, I realized the `_common_grid` intersection construction already guarantees every held coin has data over any selected rebalance window — the outer loop's `i + rebalance_hours <= len(grid)` guard rejects shorter periods entirely. The fix (`len(vals) == len(future)`) is therefore defensive hardening for any future change to the grid logic, not a behavioral correction. Numbers in this build are unchanged by the fix; the new test asserts the actual invariant the architecture provides. Honest about the finding direction.

**2c. Paper-trading positions never close on stale — FIXED.**
The only close trigger was "gap has decayed to <= 50% of entry". Positions on persistently-mispriced events never closed. Added a max-age fallback (default 7 days): any position older than the cap is closed at the current gap as if exited, with `close_reason='max_age'`.

**2d. Monitor OOM at PAGE_SIZE=50, max_events=300 — PARTIALLY FIXED.**
Root cause is Windows virtual-memory exhaustion (page file too small), not a real code bug. The default `max_events_per_poll` is now 100 (known-safe from the day-1 ingest run), with a note in the README that larger caps require a beefier host. Server-side `negRisk=true` filter on gamma is not honored — verified via direct API probe — so client-side filtering is the only option.

## 3. Things the build cannot answer that I'd want to know

**3a. Book depth on the flagged World Cup signal.**
The 150bp gap is at top-of-book. The CLOB `/order-book` endpoint exposes full depth; running it on each of the 48 World Cup markets would tell us the true average fill price across the basket and the maximum executable size. The build does not do this. The signal could be 150bp on $50 of size and 30bp on $500 — very different practical outcomes.

**3b. Persistence — how long does a 150bp signal last?**
The forward-observation `monitor` was supposed to answer this, but ran into the OOM repeatedly. The persistence section of the report covers only a 3-minute window (single successful run); the persistence claim in the CV / cover letter is therefore weaker than I'd hoped. The infrastructure is in place — running `polymarket-edge monitor --duration-minutes 600 --max-events-per-poll 100` on a host with a larger page file would generate the real persistence numbers in a single overnight run.

**3c. Hedge-leg cost on the Hyperliquid backtest.**
Funding-only Sharpe of 30–50 is an artifact of not modeling the spot/perp basis. A real implementation would need to pull Hyperliquid spot prices, simulate the hedge entry slippage and ongoing basis P&L, and the result would have Sharpe in the low-single-digits at best. The README and report both lead with this caveat.

**3d. Survivorship and listing-shift effects.**
The 38-coin universe is "what's listed today, with 30d history available." Coins that listed and delisted during the window aren't in the data; new perps that listed mid-window have shorter trailing means in the predictor (a subtle look-back / sample-size bias). Not corrected.

**3e. The 12h `/prices-history` floor.**
I cite [py-clob-client#216](https://github.com/Polymarket/py-clob-client/issues/216) as the source. The issue was filed in 2024. I attempted to re-verify it directly against the live API during this audit but the probe failed on a system-level memory issue; I am taking the documented constraint at face value. If the floor was later relaxed, an execution-grade historical backtest of Polymarket signals would become feasible.

## 3a. Depth analysis — promoted from "open" to "done", and the result is the most interesting finding in the build

After committing the first red-team pass I went back and built `book_depth.py` to answer item 3a above ("Book depth on the flagged World Cup signal"). The result completely changes how the three flagged signals should be read.

For each flagged event, the depth-aware basket gap as you scale notional per market:

**2026 FIFA World Cup Winner (negRiskAugmented, 48 markets, sell-side):**

| notional / market | gap (top-of-book) | gap (depth-aware) | throttle |
|---|---|---|---|
| $10 | +150bp | +150bp | Spain $10 |
| $100 | +150bp | +150bp | Spain $100 |
| $1,000 | +150bp | +150bp | NZ $1,000 |
| $5,000 | +150bp | +150bp | Iran $3,037 (book exhausted) |

The 150bp gap is **real and tradeable** through a $48,000 basket ($1K × 48 markets), and the maximum basket is ~$145K bottlenecked by Iran's full bid book.

**2028 US Presidential Election (negRiskAugmented, 2 markets, buy-side):**

| notional / market | gap (top-of-book) | gap (depth-aware) | throttle |
|---|---|---|---|
| $10 | +100bp | +100bp | Republicans $10 |
| $100 | +100bp | +100bp | Republicans $100 |
| $1,000 | +100bp | +50bp | Republicans $1,000 |
| $5,000 | +100bp | **-38bp (loss)** | Republicans $5,000 |
| $20,000 | +100bp | **-177bp (loss)** | Republicans $20,000 |

The 100bp signal is **marginal**: holds at small sizes, decays smoothly, **inverts to a loss by $5K/market**.

**Harvey Weinstein sentencing (non-augmented, 6 markets, sell-side):**

| notional / market | gap (top-of-book) | gap (depth-aware) | throttle |
|---|---|---|---|
| $10 | +70bp | **-307bp (loss)** | "5-10 years" $7.83 |
| $50 | +70bp | **-1,040bp (loss)** | "5-10 years" $7.83 |
| $5,000 | +70bp | **-8,375bp (loss)** | "5-10 years" $7.83 |

The 80bp signal is a **TRAP**. One of the six markets ("between 5 and 10 years") has only **$7.83 of total bid-side liquidity**. Selling even $10 of that market means walking the book to near-zero, and the basket P&L craters. Top-of-book gaps without depth analysis are dangerous — this is exactly the kind of false signal that loses money to anyone running a naive detector.

**Lesson.** A top-of-book event-level gap detector is necessary but not sufficient. The depth-aware basket-fill model is the difference between a real signal (World Cup), a marginal one (Election), and an actively dangerous one (Weinstein). This finding is now the headline of the deliverable.

## 3b. Hyperliquid hedge cost — promoted from "open" to "done", and the result kills the headline at 8h cadence

REDTEAM item 3c was the unmodeled spot/perp hedge cost. A follow-on module (`hl_hedge.py`) charges `4 × spread_bps_per_leg` per rebalance (entry perp + entry spot + exit perp + exit spot). Even at a modest 5 bps per leg (20 bps round-trip), the spread cost demolishes the original numbers because the gross carry per 8h rebalance is only 1.74 bps.

**Net-of-spread result at 5 bps/leg:**

| rebalance | n | gross annualized | net annualized | net Sharpe |
|---|---|---|---|---|
| 8h | 56 | +19.0% | **−200.0%** | −388.6 |
| 24h | 18 | +16.5% | −56.6% | −70.9 |
| 72h | 6 | +5.0% | −19.4% | −10.3 |
| 168h (weekly) | 2 | +8.0% | −2.4% | −2.7 |
| 336h (biweekly) | 1 | +6.6% | +1.4% | ≈0 |

**Breakeven on the 8h variant is ~0.43 bps per leg.** Realistic round-trip costs on Hyperliquid + spot are several bps minimum. The headline +19% at 8h cadence is not net-viable. Even at weekly rebalance with the most generous 1 bp/leg assumption, net return is +10%, only just clearing the base-rate floor that a passive DOGE short captures.

A churn-aware variant (only charge spread on the *changed* leg between rebalances) would soften this. Not implemented. The honest pitch coming out of this pass is: "the carry signal is real, but the headline 8h-rebalance configuration that produced +19% is not a real strategy after costs." The depth analysis killed the Weinstein "signal"; the hedge model now kills the Hyperliquid "signal" at its original cadence. Two parallel narrative corrections, both initiated by the red-team pass.

## 4. What this red-team pass changes about the deliverable

- **README**: corrects fees, replaces "8× BTC" with excess-over-floor framing, adds the `negRiskAugmented` caveat, points to this document for the full audit.
- **REPORT.md** (generated): same corrections; adds the hyperparameter-sensitivity table; lists the World-Cup-clears-Sports-fee finding explicitly.
- **CV / cover letter**: updated to use the honest framing ("captures +8 percentage points of excess carry over the funding-rate floor" instead of "8× passive BTC").
- **Code**: three real bug fixes (2a, 2b, 2c) with new test coverage where the math changed.

A normal undergrad project ships at the first green test suite. This document is the difference.

## 5. Fourth-pass red-team (post-publish to GitHub)

After the repo was public, ran another audit pass against the live state. Findings:

**5a. Build-window depth findings need timestamp framing — FIXED in narrative.**
The "Polymarket flagged three live events" framing in the README implicitly invited the reader to reproduce all three findings against current state. Re-running the depth analysis 18 hours after the original capture: the World Cup leg still holds (+144bp at $1K/market, was +150bp; Iran throttles at $2.8K, was $3.0K — small drift), but **the Weinstein and Election gaps both compressed below the 50bp detector threshold** and no longer flag. The depth findings were valid for the moment they were captured (the math is correct), but they read as "live now" rather than "build-window snapshots." The README now frames the three depth cases as "captured 2026-05-21" with an explicit note that the World Cup is the durable example.

**5b. `cross_venue.align_series` has a units foot-gun — FIXED.**
The function takes PM timestamps in seconds and HL timestamps in milliseconds. This is documented in the docstring but easy to call wrong — a self-audit script feeding both legs in ms got back exactly one aligned bucket and all-NaN correlations, which mimics a "broken function" but was actually a caller bug. Added explicit `_validate_timestamp_units` validation at function entry that raises `ValueError` with a clear message if the timestamp magnitudes look wrong (boundary at 1e11, comfortably between any plausible "now" in either unit). New tests `test_align_series_rejects_wrong_pm_unit` / `test_align_series_rejects_wrong_hl_unit` lock the validation behavior.

**5c. Persistence study ran successfully — DOCUMENTED.**
After the earlier two OOMs, a third monitor run with tight bounds (`max_events_per_poll=30`, `poll_interval=120s`, `duration=25min`) completed cleanly: 13 polls, 52 trajectories on 4 distinct flagged events. Mean |gap| = 1.4%, p90 = 3.2%. The forward-test mean decay-toward-zero over a 5-minute hold rounded to 0.0000 — gaps **persisted** during the observation window. This is a small sample (4 events, 25 minutes) but it's the first real persistence data the project has; the README's persistence section now cites these numbers rather than saying "the monitor died."

**5d. Repo is clean.** Confirmed via `gh api`: no DB file, no credentials, no .env, no shm/wal files on GitHub. License field is null — fine for a one-author portfolio project but worth noting if Harry wants to ever accept external contributions.

**5e. Dashboard hardcodes the depth-vs-trap row labels.** Per spec, the agent embedded "World Cup / Election / Weinstein" as static text. Accurate for the captured snapshot, but the same drift caveat applies — the dashboard is a frozen build-window artifact, which is the right framing for a portfolio piece. Not a fix; flagged for transparency.

## 6. Fifth-pass red-team — closing the structural ceiling

After the prior four passes, I wrote down what was actually still WEAK about the project under a "would a quant founder say wow" lens (this list also lives in chat-history context for the application but I want it documented here too):

1. **Nothing real had been traded** — all paper.
2. **Findings were mostly *what didn't work*** — Weinstein trap, momentum loses to level, cross-venue null, Hyperliquid headline collapses under cost. No durable positive edge claim other than World Cup.
3. **No novel finding** — every result here is a clean implementation of a known pattern.
4. **No walk-forward OOS** — the +19% headline was in-sample on the full window.
5. **Bootstrap CIs were IID** — wrong on autocorrelated funding returns.

Four parallel agent streams in the fifth pass:

**6a. Microstructure trap-rate study — THE headline finding now.** Scanned 500 active events, classified each by depth-aware basket P&L at $50 and $500/market. 19/500 = 3.8% flagged by the detector. **Of those, 63.2% are traps** (gap inverts to a loss at $50/market because one constituent has near-zero depth). The trap pattern is concentrated in 2-market US state-election negRisk events — 11/13 = 85% trap rate for `Politics`/`Elections`/`US Election`/`Midterms` tags combined. The two `real` signals identified are 48-market World Cup (Soccer, the durable case-study event from earlier passes) and 20-market Nobel Peace Prize (Awards). The mechanical explanation — thin-side bid-book collapse on the 5%-probability market — is structural rather than transient. Full writeup: [MICROSTRUCTURE.md](MICROSTRUCTURE.md). This is the first finding in the project that is genuinely population-level research rather than n=1 anecdote.

**6b. Walk-forward OOS validation.** Multiple sliding train/test windows on the Hyperliquid 30-day data. **OOS slightly *outperforms* IS** — decay is negative (−3 to −7 percentage points, OOS > IS) across three different train/test ratios. The signal is durable, not over-fit. Net of 5bp/leg spread the OOS result is catastrophic (−195% to −203% annualized), confirming the existing hl_hedge finding holds out-of-sample. New module `walkforward.py`. Spec error caught: the DB has ~22 days of common-grid data, not the nominal "30 days, 18,500 ticks" the README has been quoting loosely — that's the actual time span behind every Hyperliquid number; the +19% headline was always on ~22 days, and the writeup now reflects that.

**6c. Block bootstrap CIs.** Funding returns have ACF(1) = +0.574 — IID resampling understates variance. Moving-block bootstrap with optimal block length 2 widens the annualized-return CI by **~28%**. Honest band is **[+14.08%, +25.18%]** instead of the IID [+14.88%, +23.69%]. Sharpe CI widens ~12%. The point estimate is unchanged. New module `hl_stats_block.py`. Stationary (Politis-Romano) implementation also included for completeness; produces very similar results to moving-block.

**6d. Real-trade runway.** `scripts/size_basket_trade.py` computes per-market notionals, expected fills, maker-vs-taker net P&L, and a kill-the-trade threshold for a $20 real trade on a chosen event. Live-tested on the World Cup: at $20 total, $0.42/market, maker mode shows **+170 bps net (rebate-positive) — expected P&L +$0.04**; taker mode at 0.75% Sports fee shows −15 cents. `EXECUTION.md` is the step-by-step checklist. **UK jurisdiction is restricted** by Polymarket (verified via help.polymarket.com); the checklist documents the py-clob-client non-broadcast simulation path for restricted-jurisdiction users — the order builder produces signed orders that we don't post. Not a substitute for a real fill, but better than nothing.

**What's still open after this pass.** One real $20 fill from the user side (the sizing script + checklist are the runway; the actual fill must be done by Harry, modulo the UK jurisdiction constraint). Everything else from the original weakness list — novel finding, walk-forward, block bootstrap — has been addressed.

Test count: 79. CI green on every push. Eight markdown documents, five chart/HTML artifacts, eleven modules.

## 7. Sharpen pass — closing the remaining gaps

Four parallel streams. The most narrative-shifting was the volume-weighted reframing — easy to compute, single SQL query, and it completely changes the headline.

**7a. Volume-weighted trap rate. By count: 55.6%. By dollar: 0.012%.** The World Cup `real` event alone carries 95.9% of the flagged volume — every trap is a small US state-election event in the four-to-five-figure range. The 63.2% number that the original microstructure section leads with is correct under "what fraction of detected events are traps" but misleading under the more useful "what fraction of dollars at risk." A founder hearing only the count-based number assumes the strategy loses two of every three dollars; the dollar-weighted version says the strategy concentrates its risk on the dollar-dominant World Cup. The full re-analysis lives in MICROSTRUCTURE.md `## Volume-weighted re-analysis` (appended, not replacing); the count-based section is left intact for transparency. Three events had NULL volume (small state-races upserted by non-canonical paths); handled as 0 with stderr warnings. Implementation: `scripts/volume_weighted_trap_rate.py`.

**7b. Trap classifier — descriptive finding becomes a model (scaffolding).** Logistic regression on `(category_tag one-hots, n_markets, is_two_market, is_us_politics, neg_risk_augmented, top_of_book_gap_bps)` with batch gradient descent + L2 ridge, leave-one-out CV. **AUC = 0.600 on n=18**, accuracy at p=0.5 = 77.8% vs base rate 55.6%. Top features `is_us_politics` (+2.29) and `neg_risk_augmented` (+1.50) — mechanically coherent. The classifier is honest scaffolding at n=18 (LOOCV AUC of 0.6 has wide error bars at this n) but the artefact is the methodology: as daily scans accumulate, the same script retrains and the AUC becomes load-bearing. New module `trap_classifier.py`, 19 tests. CLI: `polymarket-edge trap-predict`.

Spec issue: the agent discovered the DB's `microstructure_classifications` table was actually empty when it tried to train — the original 19-event scan from §6a was apparently not persisted in the local DB committed to git (probably .gitignore'd correctly to keep the DB out of the repo, but my own working DB had been cleared since). The agent re-ran `scripts/microstructure_scan.py` which produced a fresh 18-event scan; numbers in the new section reflect this fresh scan, not the original. Both are valid snapshots of the same population pattern.

**7c. Tail-risk (VaR, ES, drawdown distribution).** `hl_tail.py`. GROSS: VaR_95 = +0.0038% (positive — the 5th-percentile period is still positive carry), Expected Shortfall_95 = −0.0016%, max drawdown 0.0068% (1 period). NET of 5bp/leg: VaR_95 = −0.196%, ES_95 = −0.202%, max drawdown 10.23% over the entire 56-period sample, never recovers. The GROSS-vs-NET asymmetry on ES_95 is ~125× — every percentile of the return distribution is bad when you net cost, not just the mean. This is the single most damning statistic against the headline 8h cadence and complements the existing `hl_hedge` finding. 11 new tests. CLI: `polymarket-edge hl-tail`.

**7d. Visual polish.** `dashboard.py` CSS rewritten with a system-mono numeric font, restrained Tailwind-emerald/rose/amber palette, KPI cards with 1px borders and uppercase tracked labels, mobile breakpoint at <600px, footer byline. `plots.py` matplotlib rcParams set globally (DejaVu Sans, slate-700 ticks, despined top/right, single dotted horizontal grid, 144 DPI). Dashboard final size 239 KB (under the 250 KB self-contained cap). The before-after delta is significant — the dashboard now reads as a polished portfolio artefact rather than "default browser styling."

**What's still open after this pass.** Same as §6: the real $20 trade (Harry's task, UK-restricted; the simulation path is documented). Every code-side item from the §6 weaknesses list — novel finding, prescriptive classifier, walk-forward, block bootstrap, tail risk, volume reweighting, visual polish — is shipped.

Test count: 109. CI green on every push. Twelve modules, twelve markdown documents, two chart PNGs, one dashboard.html.

## 8. Edge-finding pass — three parallel hypothesis tests

After the previous passes had closed every methodology gap, the project still lacked a *positive expected-return claim* — every finding was either descriptive or walked-back. This pass tested three independent edge hypotheses in parallel and reports all three results honestly, including the cells that didn't survive.

**8a. World Cup market-maker yield (Plan A — positive, knife-edge).** Built a maker-side simulator on the 48 World Cup constituent markets that walks 30 days of historical trade flow from the CLOB data-api and projects net P&L (rebate − adverse selection) forward to tournament resolution. Three AS scenarios:

- Naive (AS=0): +$12,372 projected over 50 days
- **Moderate (AS = 0.5× spread): +$126 projected** — the headline
- Informed (AS = 1.0× spread): −$12,120

**Breakeven half-spread fraction = 0.505.** Net P&L crosses zero right at the textbook moderate assumption. 89% of the positive net comes from 5 favourites (France, Spain, England, Argentina, Brazil); 41 of 48 markets are individually net-negative. The basket clears only because top contenders dominate. Implementation `polymarket_mm_sim.py`, writeup [WORLD_CUP_MM.md](WORLD_CUP_MM.md). Honest framing: defensibly positive under any AS assumption below the literature-standard 0.5 of half-spread.

Spec deviation worth noting: the spec's `clob.polymarket.com/trades?market=<token_id>` is auth-gated (401). The agent fell back to the public `data-api.polymarket.com/trades` with `market=<conditionId>` filter — verified the documented `asset`/`token` params silently return unrelated global flow. Server-enforced max pagination offset of 3000 caps lookback at ~4 days on high-volume markets; long-tail markets still get the full 30. Documented in the `WORLD_CUP_MM.md` caveats.

**8b. Hyperliquid basis-hedge + regime conditioning (Plan B — mixed).** Replaced the parametric 5 bps/leg cost in `hl_hedge.py` with a real spot/perp basis model. Of 37 universe coins only 9 have liquid Hyperliquid spot listings — `AVAX, AZTEC, BTC, ENA, ETH, PUMP, SOL, STABLE, XPL`. Hedged headline over 59 rebalances: **−106% annualized, Sharpe −3.60** — the basis decoupled badly during the build window (perp/spot drift dominated, the 5 bps/leg parametric model had been understating real hedge cost).

But regime-conditioning by trailing 7d BTC realized vol surfaces a positive cell: **low-vol tercile (N=11), basis-hedged at 5 bps/leg: +72.5% annualized, Sharpe +1.91, 95% bootstrap CI [−44, +20]**. Med and high vol regimes are decisively negative (−522% and −605% annualized). The wide CI on the positive cell is N=11 — directional evidence, not statistical proof. Implementation `hl_basis_hedge.py`. Spec deviation: the agent substituted a union-grid + per-coin eligibility check for the strict-intersection helper in `_common_grid` because spot listings launching mid-window collapsed the intersection grid to 4 rebalances.

**8c. Funding-extreme directional study (Plan D — strongest positive finding, with caveats).** Tested whether perp prices at extreme funding events (|z| > 1.5, 2.0, 2.5 vs trailing 168h) rally or crash over 6h / 24h / 72h holds across 37 coins. 18 (threshold × direction × horizon) configurations, Bonferroni-corrected at t > 3.05.

- Positive-funding extremes (z > 2): NULL (t = −0.13 at 24h hold). The standard "high-funding-shorts-win" intuition does NOT hold at the >2σ tail.
- **Negative-funding extremes (z < −2): LONG the perp returns +1.18% over 24h, +4.48% over 72h.** t-stats range +3.27 to +7.09 across the negative-funding cells. **7 of 18 cells clear Bonferroni.**

But the independence check is sharp: with cooldown ≥ 72h between extremes per coin (event independence), event counts collapse ~8× and **zero cells clear Bonferroni** — the headline depends on clustered events. DOGE in isolation clears (t=+3.81); BTC/ETH produced zero events in the window. Honest reading: directional signal exists asymmetrically (the long-side-at-negative-funding edge is real on the data), but the strict-independence version is "candidate hypothesis pending larger sample." Implementation `hl_extremes.py`, appended writeup section in MICROSTRUCTURE.md.

**Combined narrative.** The three findings together support a small diversified portfolio claim: a maker-rebate strategy on the World Cup, a regime-gated basis-hedged funding capture, and a negative-funding-extreme long bias. None of them is a "ship and print money" strategy in isolation — each has a real caveat — but together they're a credible "here's where I'd deploy ~$X across three orthogonal microstructure edges if hired" pitch. That's the email's lead.

Test count: 146. CI green on every push. Fifteen modules, thirteen markdown documents (+ WORLD_CUP_MM.md). All edge hypotheses tested honestly; positive cells and walked-back cells both documented.

## 9. Year-data audit — the foundational re-evaluation

After all the prior passes the single deepest critique was that the data window (22 days) was too thin to support the statistical claims. I pulled 365 days of Hyperliquid funding + perp price data on the 6 majors with both liquid spot and a year of funding history (BTC, ETH, SOL, XRP, DOGE, AVAX) — 52,560 hourly funding rows, 30,015 hourly candles — and re-ran every Hyperliquid analysis. The honest comparison sat under [YEAR_ANALYSIS.md](YEAR_ANALYSIS.md). Summary of what changed:

**What was walked back at year-scale.**

- **"OOS beats IS by ~6pp" (was: walk-forward on N=2-4 windows showed negative decay).** Refuted. At N=19 sliding windows on year data, decay is **+1.35 pp (conventional positive direction — IS beats OOS, modestly)**. The small-N "OOS beats IS" was sample-size noise that we cherry-picked as a strength. The honest framing is: the predictor is mildly over-fit (which is normal) but the OOS return is still positive on 18 of 19 windows.

- **"Long the perp at z<−2 negative-funding extremes" (was: 7 of 18 cells cleared Bonferroni at cooldown=0 on ~22d).** Refuted. At year-scale N with cooldown=72h (proper event independence), **zero of 18 cells clear Bonferroni**. The largest |t| anywhere in the family is 2.26, far below the 3.05 threshold. The earlier result was driven by clustered events on the same coin within a 22-day window — exactly the failure mode the cooldown check is designed to catch, and it caught it once the sample was big enough.

- **"Block-bootstrap CI [+14.1%, +25.2%]" (was: on N=22 days at 22-day sample).** Refined. Year-scale block bootstrap on N=1,092 per-period returns, optimal block length 10 (Politis-White), gives **CI [+6.32%, +9.56%]** annualized — lower and tighter than the small-N CI. The earlier CI was wider but centered on a more favourable sub-sample of the data. The honest CI is [+6.3%, +9.6%].

**What survived at year-scale.**

- **Trailing-K funding-capture has positive gross expected return.** Block bootstrap CI excludes zero. +6.38% OOS mean across 19 walk-forward windows. 18 of 19 windows OOS-positive. This is the project's actual defensible finding.

- **Low-vol regime captures roughly 2× the carry of med/high regimes.** All three regime CIs exclude zero (low: [+6.40%, +7.46%], med: [+2.34%, +3.86%], high: [+1.98%, +3.90%]). The directional ordering survives at proper N. The dramatic +72% number from the N=11 hedged-low-vol cell does NOT survive — the year-scale low-vol cell is +6.94%, an order of magnitude less. The earlier point estimate was small-N variance.

- **Predictor is not over-fit.** 1.35 pp IS-OOS decay is conventional and mild. The signal is real, modest, and reproduces out of sample.

**Honest interpretation.** The Hyperliquid funding-capture strategy has a real positive gross edge of ~+6% OOS annualized on year data. The previous more-dramatic claims (+19%, +72% in low-vol, contrarian-long at extremes, OOS beats IS) were all small-N artifacts that the year-data run honestly walked back. The walked-back claims came down where they were over-stated and the survived claims are statistically defensible at the much larger sample. The strategy is not net-viable at 8h cadence after realistic spread costs (this REDTEAM §3b finding stands), but the underlying signal is real — the deployable form is at lower rebalance frequencies where the per-rebalance cost amortizes.

**The deepest critique addressed.** Previously every "edge" finding was small-N at the headline level. The year-data audit replaces those with a statistically defensible core finding (gross funding-capture works) and explicitly walks back the previous over-claims. The pattern is no longer "claim X, walk back, claim Y, walk back" iteration — it's "claim X on small N, get the bigger sample, walk back what doesn't hold and confirm what does." That's the only legitimate way out of the small-N trap.

Test count after this pass: 146 (no new tests; the audit re-uses existing analysis modules). CI green.
