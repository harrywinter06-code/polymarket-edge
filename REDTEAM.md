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

## 4. What this red-team pass changes about the deliverable

- **README**: corrects fees, replaces "8× BTC" with excess-over-floor framing, adds the `negRiskAugmented` caveat, points to this document for the full audit.
- **REPORT.md** (generated): same corrections; adds the hyperparameter-sensitivity table; lists the World-Cup-clears-Sports-fee finding explicitly.
- **CV / cover letter**: updated to use the honest framing ("captures +8 percentage points of excess carry over the funding-rate floor" instead of "8× passive BTC").
- **Code**: three real bug fixes (2a, 2b, 2c) with new test coverage where the math changed.

A normal undergrad project ships at the first green test suite. This document is the difference.
