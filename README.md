# polymarket-edge

[![ci](https://github.com/harrywinter06-code/polymarket-edge/actions/workflows/ci.yml/badge.svg)](https://github.com/harrywinter06-code/polymarket-edge/actions/workflows/ci.yml)

Event-level no-arb scanner for Polymarket mutually-exclusive (`negRisk`) markets, plus a Hyperliquid funding-capture backtest. Built in five days as ammunition for an Ask Gina quant-intern application.

A red-team self-audit of every claim below lives in [REDTEAM.md](REDTEAM.md). Read that file before trusting any number. The product framing for Ask Gina specifically — what shippable recipes follow from these findings — lives in [RECIPES.md](RECIPES.md) and [GINA_ENGAGEMENT.md](GINA_ENGAGEMENT.md). A cross-venue case study (Fed-rate-cuts Polymarket market vs BTC perp funding, null result, rigorous method) lives in [CROSSVENUE.md](CROSSVENUE.md). **The headline empirical finding — that 63% of detector-flagged negRisk events are unprofitable traps, concentrated in 2-market US state-election races at 85% trap rate — lives in [MICROSTRUCTURE.md](MICROSTRUCTURE.md).** A single-file [`dashboard.html`](dashboard.html) renders the headline numbers and charts in a browser without external dependencies. [`EXECUTION.md`](EXECUTION.md) documents the runway for a real $20 trade including the UK-jurisdiction non-broadcast simulation path.

## What it does

**Polymarket leg.** `P(YES) + P(NO) = $1` is contract-enforced per market via the CLOB order-mirroring rule — every buy of YES at price *p* is simultaneously visible as a sell of NO at `1 - p`. Intra-market arbs are competed out in steady state, so the non-trivial signal lives at the **event** level. For a `negRisk` event with *N* mutually-exclusive markets, the sum of YES probabilities across the event must equal 1.0 in fair pricing. Deviations imply tradeable arb:

- `sum(best_bid_yes) > 1`: **sell-side** — sell YES across all markets; exactly one settles at $1.
- `sum(best_ask_yes) < 1`: **buy-side** — buy YES across all markets; exactly one settles at $1.

The scanner ingests every active event from the gamma API, scores every `negRisk` event, and flags deviations exceeding a configurable fee buffer. A forward-observation `monitor` records signal trajectories; the `persistence` and `forward-test` analyses measure how quickly flagged signals decay toward fair.

**Hyperliquid leg.** The info endpoint exposes hourly funding per perpetual and an unconstrained historical series via `fundingHistory`. The backtest tests a top-K trailing-window funding-capture strategy: at each rebalance tick, rank coins by trailing-mean funding, short the top K (equal-weight), realize the actual funding flow over the next interval. Benchmarks: perfect-hindsight (look-ahead top-K) and passive-short on a chosen coin.

## Results (with sensitivity)

## Polymarket microstructure — trap rate by count, but not by dollar

The single most surprising finding in the project comes from the same data viewed two ways.

**By count:** scanned 500 active Polymarket events; the top-of-book event-level no-arb detector flagged **19** at a 50bp fee buffer. The depth-aware classifier (`microstructure.py`) walks the full `/book` on every market in the flagged direction at $50 and $500/market and classifies each event:

| verdict | count | share |
|---|---|---|
| real (clears 50bp at $500/market) | 2 | **10.5%** |
| marginal (clears at $50, decays into fee buffer by $500) | 5 | 26.3% |
| **trap (gap inverts to a loss at $50/market)** | **12** | **63.2%** |

**The pattern: traps concentrate in 2-market US state-election negRisk events.**

| category | total flagged | trap | trap rate |
|---|---|---|---|
| Politics | 6 | 5 | **83.3%** |
| Elections | 4 | 3 | **75.0%** |
| US Election | 3 | 3 | **100%** |
| Midterms | 2 | 1 | 50.0% |
| Soccer | 1 | 0 | 0% |
| Awards | 1 | 0 | 0% |

Mechanical explanation: a 2-market state race (e.g. governor's seat: Democrat vs Republican) has one market at ~5% probability whose entire bid book is single-digit dollars. The detector reads `bestBid(5%-side) + bestBid(95%-side) = 1.01` and flags +100bp sell-side, but at any meaningful basket size the thin side's book collapses and the basket walks to a deep loss. The detector flags exactly the events that are LEAST tradeable. The two `real` signals were 48-market World Cup (Soccer) and 20-market Nobel Peace Prize (Awards) — both events with liquidity spread across many legs.

**By dollar:** the same 18 flagged events ($1.15B total lifetime volume), volume-weighted, give a trap rate of **0.012%**. The 2026 FIFA World Cup `real` event alone carries **95.9% of the flagged volume** ($1.10B of $1.15B). Every trap is a small US state-election event in the four-to-five-figure volume range. Count-based and dollar-weighted views differ by 4,500×.

**Practical takeaway for any prediction-market quant:** the depth-walking pass is non-optional before sizing, AND naive count-based statistics massively overstate the trap risk to dollars-at-risk. A maker-only sizing pipeline anchored on the World Cup-style high-volume events captures most of the dollar opportunity while almost entirely avoiding the trap-prone long tail of small state-race events.

**Trap classifier (n=18, scaffolding):** trained a logistic regression on `(category tag, n_markets, top-of-book gap, is_US_politics, neg_risk_augmented)` with leave-one-out CV: **AUC = 0.600**, accuracy at p=0.5 = 77.8% vs base rate 55.6%. Top features `is_us_politics` (+2.29) and `neg_risk_augmented` (+1.50). The model is honest scaffolding at n=18; once daily scans accumulate to n>100 the same script retrains and the AUC becomes load-bearing. Run via `polymarket-edge trap-predict`.

Full method + caveats + the binary-classification jitter discussion + the volume-weighted reframing + the classifier section in [MICROSTRUCTURE.md](MICROSTRUCTURE.md).

**Polymarket — depth-aware case studies, captured 2026-05-21.** Across 100 active events / 1,440 markets / 18 `negRisk` events scored on the build-window snapshot, three real microstructure deviations at the 50bp threshold — but **only one of them actually trades**:

| event | n_mkts | top-of-book gap | gap at $1K/mkt | tradeable? |
|---|---|---|---|---|
| 2026 FIFA World Cup Winner | 48 | +150bp sell | **+150bp** | **YES** — $48K basket, $145K max before Iran throttles |
| 2028 US Election party | 2 | +100bp buy | +50bp, **inverts at $5K** | marginal — small size only |
| Harvey Weinstein sentencing | 6 | +80bp sell | **−1,040bp at $50/mkt** | **TRAP** — one market has $7.83 total bid depth |

This is the core finding of the project. A top-of-book gap detector flags all three. A *depth-aware* basket model — `book_depth.py`, which walks each market's full `/book` and computes the basket-trade average fill — separates the real signal (World Cup, executable at meaningful size and clearing the 0.75% Sports taker fee) from the marginal one (Election, fee-clearable only at retail size) from the trap (Weinstein, where the naïve top-of-book reading would lose money instantly).

**These three cases are moment-in-time depth analyses.** Top-of-book gaps shift hourly with market activity. As of a re-snapshot 18 hours after the original capture, the World Cup leg still holds at +144bp at $1K/market (Iran throttles at $2.8K); the Weinstein and Election gaps have both compressed below the detector's 50bp threshold. The World Cup case is the durable one; Weinstein/Election are the *kind* of patterns the detector + depth model surface, captured for the writeup at the moment they were flagged. Run `polymarket-edge depth <slug>` to verify against current state.

**Hyperliquid — GROSS backtest sensitivity (30d, 18,500 hourly ticks, 38 perps).**

| top_K | trail | rebal | n | annualized | Sharpe | hit% |
|---|---|---|---|---|---|---|
| 3 | 24h | 8h | 56 | **+21.5%** | +28.7 | 92.9% |
| 5 | 24h | 8h | 56 | +19.0% | +37.0 | 98.2% |
| 10 | 24h | 8h | 56 | +14.9% | +49.5 | 98.2% |
| perfect-hindsight K=5 | — | 8h | 59 | +22.3% | +39.5 | 100.0% |
| passive short BTC | — | 8h | 62 | +2.3% | +13.4 | 66.1% |

Gross decomposition of the +19.0% top-5: ~11.0% comes from the base-rate funding floor (interest-rate component, ~10.95% APR — any coin near zero premium pays shorts this passively). The remaining ~8.0 percentage points are the selection excess from the trailing-mean predictor. The trailing-24h variant recovers ~85% of the perfect-hindsight K=5 ceiling.

**Bootstrap 95% CI on the headline (N=56 rebalances):**

| method | annualized return CI | Sharpe CI |
|---|---|---|
| IID (naive) | [+14.88%, +23.69%] | [+30.24, +52.83] |
| **Moving-block (optimal block=2)** | **[+14.08%, +25.18%]** | [+30.38, +55.20] |
| Stationary bootstrap | [+13.92%, +25.35%] | [+30.37, +56.30] |

Funding returns are autocorrelated (ACF(1) = +0.574) — IID resampling under-states the variance. Block bootstrap widens the annualized-return CI by **~28%** and the Sharpe CI by ~12%. The honest defensible band is **[+14.1%, +25.2%]**, not the IID [+14.9%, +23.7%]. Run via `polymarket-edge hl-ci-block`.

**Tail risk** on the headline strategy (`polymarket-edge hl-tail`):

| metric | GROSS | NET (5bp/leg) |
|---|---|---|
| annualized return | +19.03% | −199.97% |
| VaR_95 (per period) | +0.0038% | −0.196% |
| Expected Shortfall_95 | −0.0016% | −0.202% |
| max drawdown | 0.0068% | 10.23% |
| max-drawdown duration | 1 period | 56 periods (entire sample) |
| recovery from max DD | 1 period | never |
| periods in drawdown | 1 of 56 | 56 of 56 |

The GROSS tail is essentially trivial — funding is so consistently positive on the trailing-K-selected coins that the worst 5% of periods is still essentially breakeven. The NET tail is the inverse: every period is a loss, drawdown is monotonic. Tail asymmetry between GROSS and NET (~125× larger tail loss) is the single most damning statistic against the headline 8h cadence — it's not just "the mean is bad," every percentile is bad. Implementation: `hl_tail.py`.

**Walk-forward (out-of-sample) validation, train=10d/test=5d/step=3d:** *the OOS result slightly OUTPERFORMS in-sample.*

| config | n windows | IS mean ann ret | OOS mean ann ret | decay (IS − OOS) |
|---|---|---|---|---|
| train=10, test=5, step=3 | 2 | +14.27% / +20.17% | +16.87% / +29.69% | **−6.06pp** |
| train=10, test=7, step=2 | 2 | +17.74% / +20.15% | +24.06% / +27.78% | **−6.98pp** |
| train=7, test=5, step=2 | 4 | +13.28% .. +23.76% | +15.73% .. +29.69% | **−3.42pp** |

Negative decay = OOS beats IS. Every coin held in train was still in the universe at test time. The selection signal is **persistent, not over-fit** — a real strength signal. The +19% headline is robust to OOS, but as already documented it does NOT survive realistic spread costs at 8h rebalance cadence (OOS net of 5bp/leg = −195% to −203%, matching the in-sample finding). Run via `polymarket-edge walk-forward`.

**Funding-momentum variant** (rank by z-score of recent vs longer-window funding rather than by level) was tested and **lost** to the level-based ranker: +8.0% annualized vs +17.2% on a matched 168h-history budget. Rate-of-change does not beat level here — clean negative result documented in `hl_strategies.py`.

**But every number above is gross of execution cost.** A second pass (`hl_hedge.py`) nets the short-perp + long-spot round-trip spread, charging `4 × spread_bps_per_leg` per rebalance. At a realistic 5 bps/leg (20 bps round-trip):

| rebalance | gross annualized | net annualized | net Sharpe |
|---|---|---|---|
| 8h | +19.0% | **−200.0%** | −388.6 |
| 24h | +16.5% | −56.6% | −70.9 |
| 72h | +5.0% | −19.4% | −10.3 |
| 168h (weekly) | +8.0% | −2.4% | −2.7 |
| 336h (biweekly) | +6.6% | +1.4% | ≈0 |

**Breakeven on the 8h variant is ~0.43 bps per leg** — below any realistic execution cost on Hyperliquid + spot. The carry signal exists, but at the headline 8h cadence it is entirely consumed by execution costs. Only weekly+ rebalance survives, and even then only at sub-5bp/leg spread.

This is the honest answer to the question "what's the Sharpe really?": **depends entirely on cadence, and the headline 8h configuration is not viable after costs**. The find is in [`scripts/spread_sensitivity.py`](scripts/spread_sensitivity.py).

## Limitations (read before trusting any number)

- **Polymarket detector** treats `negRisk: true` as mutually exclusive *and* exhaustive. The `negRiskOther` market breaks exhaustivity; the detector records its presence but does not adjust the sum constraint. `negRiskAugmented: true` events (e.g. the World Cup, 2028 Election) allow new outcomes to be added mid-event, softening the strict sum=1 bound. Weinstein is NOT augmented, so its 80bp signal is structurally cleaner than the World Cup's 150bp.
- **Fee model.** Polymarket fees are per-category and probability-curved (peaked at 50%), not the flat 2% I initially assumed. Sports 0.75%, Politics 1.0%, Geopolitical 0%, Culture ~1.25%, Crypto 1.8%, Makers 0% + 20-25% rebate. The "fee-clearable" column above is taker-side; maker-only execution clears all listed gaps.
- **Detector vs depth.** The event-level `detector` reads top-of-book only — useful for flagging candidates, but it cannot tell a real signal from a trap. The `book_depth` module is what makes the signal actionable; the depth pass is mandatory before any size sizing.
- **No historical Polymarket backtest.** CLOB `/prices-history` floors at 12h granularity for resolved markets ([py-clob-client#216](https://github.com/Polymarket/py-clob-client/issues/216)), so an execution-grade historical backtest is infeasible. The forward-observation persistence study fills the gap. Best run so far: **52 trajectories over 13 polls / 25 minutes** on 4 distinct flagged events (after several earlier runs OOMed on the host page file). Mean `|gap|` = 1.4%, p90 = 3.2%, max 3.2%. The decay-toward-zero over a 5-minute hold averaged effectively zero — the flagged gaps **persisted** during the observation window rather than decaying away, which is what you'd want for tradeability but it's a 25-minute / 4-event sample, not strong evidence. A multi-day run on a host with more virtual memory would settle it.
- **Hyperliquid backtest** had hedge-leg cost modeled in a follow-on pass (`hl_hedge.py`): at 5 bps per leg (20 bps round-trip) the headline +19% becomes **−200% annualized at 8h cadence**. The carry signal is genuinely consumed by execution costs at the original rebalance frequency. Salvageable only at weekly+ rebalance. Coin universe is "currently listed with 30d history available" — listing/delisting survivorship not corrected.
- **Sample size.** 30 days = ~56 rebalances. Sharpe on N=56 is noisy; confidence intervals are wide.
- **Pattern novelty.** NegRisk event-level arbitrage is a known pattern; a public Go SDK ships a `find-negrisk-opportunities` example, and there's at least one arXiv paper on the topic. This is a clean, defensible, public-API-only Python implementation with sensitivity analysis and an explicit red-team audit — not novel research.

## Cross-venue null finding (Fed-rate-cuts vs BTC)

Paired the live `how-many-fed-rate-cuts-in-2026` event (YES of "no cuts in 2026") with the BTC perp on Hyperliquid over 30 days. Hypothesis: shifts in implied probability of Fed easing should propagate to BTC via the risk-on channel.

61 aligned 12h buckets. Pearson lead-lag:

| lag | direction | r |
|---|---|---|
| 0 | contemporaneous | −0.06 |
| +3 | PM leads BTC by 36h | **+0.24** |

The +0.24 at lag=+3 is roughly 1.9σ single-test on N≈60 and doesn't survive Bonferroni across nine lags. **Null finding.** The window also contained zero scheduled FOMC announcements, which is exactly when this kind of propagation should concentrate — methodologically weak setup. Full writeup, including the "why a null is itself useful" framing, in [CROSSVENUE.md](CROSSVENUE.md).

## Architecture

```
gamma API           CLOB API              info endpoint
    │                  │                     │
    ▼                  ▼                     ▼
fetch.py        historical.py          hyperliquid.py
    │                  │                     │
    └────── db.py (SQLite, WAL) ─────────────┘
                       │
       ┌───────────────┼──────────────────────┐
       ▼               ▼                      ▼
  detector.py      monitor.py             hl_backtest.py
  (negRisk         (timestamped           (top-K trail /
   sum-of-YES      trajectories per       perfect-hindsight /
   detector)        observation run)       passive)
       │               │                      │
       └─── analysis.py / paper.py ───────────┘
                       │
                       ▼
                  report.py → REPORT.md
```

## Setup

```bash
uv sync
uv run polymarket-edge ingest          # pull + persist active events
uv run polymarket-edge scan            # score every negRisk event, persist + print top
uv run polymarket-edge monitor \
    --duration-minutes 30 --poll-interval 90 --max-events-per-poll 100
uv run polymarket-edge persistence     # forward-test + decay analysis
uv run polymarket-edge hl-ingest       # snapshot Hyperliquid funding for all 230 perps
uv run polymarket-edge hl-history \
    --coins BTC,ETH,SOL --days 30      # pull historical funding
uv run polymarket-edge hl-backtest     # run the funding-capture backtest
uv run polymarket-edge depth <slug>    # walk the book on every market in a flagged event
uv run polymarket-edge paper-auto              # one round of paper-trading
uv run polymarket-edge hl-ci                   # IID bootstrap 95% CIs
uv run polymarket-edge hl-ci-block             # block bootstrap CIs (autocorr-aware)
uv run polymarket-edge walk-forward            # OOS validation
uv run polymarket-edge microstructure-scan     # scan all flagged events, classify, aggregate
uv run polymarket-edge trap-predict            # train logreg + LOOCV AUC on the latest scan
uv run polymarket-edge hl-tail                 # VaR, ES, drawdown distribution
$env:PYTHONPATH="src"; python scripts/volume_weighted_trap_rate.py  # the 0.012% reframing
uv run polymarket-edge report                  # write REPORT.md (+ chart PNGs)
uv run polymarket-edge dashboard               # write dashboard.html
$env:PYTHONPATH="src"; python scripts/cross_venue_case.py  # cross-venue case study
$env:PYTHONPATH="src"; python scripts/size_basket_trade.py --slug <slug> --total-usd 20 --maker
uv run pytest                                  # 109 tests
PYTHONPATH=src python scripts/sensitivity.py  # backtest hyperparameter sweep
```

## Day-by-day build

- **Day 1.** Polymarket gamma + CLOB endpoints verified live. SQLite schema. Async paginated ingestion. Event-level no-arb detector. CLI: `ingest`/`scan`/`stats`. 9 tests.
- **Day 2.** `signal_trajectories` table. `monitor` polling loop tagged by run ID. `persistence_stats` / `threshold_counts` / `forward_test` analysis. CLI: `monitor`/`persistence`/`runs`. 5 tests.
- **Days 3–4.** Hyperliquid info-endpoint fetcher. `hl_funding_snapshots` + `hl_funding_history`. Three backtest strategies (trailing-mean, perfect-hindsight, passive-short). CLI: `hl-ingest`/`hl-history`/`hl-backtest`. 7 tests.
- **Day 5.** Paper-trading engine (`paper.py`). Research-note generator (`report.py`). CLI: `paper-auto`/`paper-pnl`/`report`. Initial README.
- **Day 5 (red-team).** Self-audit pass. Three real fixes (silent error swallow in `insert_funding_history`, max-age close trigger in paper-trading, monitor default cap), one defensive hardening (partial-data check in trailing backtest), four narrative corrections (fee model, "8× BTC" framing, `negRiskAugmented` caveat, pattern novelty). 4 new tests (25 total).
- **Day 5 (depth pass).** Built `book_depth.py` to answer the open question from the red-team: "is the World Cup signal actually tradeable?" Walks the full `/book` for every market in a negRisk event and computes the depth-aware basket-fill. Result: the World Cup gap holds through ~$48K of basket notional; the Weinstein signal is a trap (one market has $7.83 of bid depth); the 2028 Election signal inverts to a loss by $5K/market. 6 more tests (31 total).
- **Day 5 (upgrade pass — parallel agent work).** Four concurrent additions: `plots.py` (chart generation, matplotlib), `hl_hedge.py` (spread-cost-net backtest; finding: +19% becomes −200% at 5bp/leg, 8h cadence not net-viable), `RECIPES.md` (Ask-Gina-specific recipe framing), and GitHub Actions CI. +14 tests (45 total).
- **Day 5 (impressive pass — parallel agent work).** Four more concurrent additions: `dashboard.py` (single-file HTML with embedded charts), `cross_venue.py` + `CROSSVENUE.md` (Fed-cuts↔BTC null finding, methodology), `hl_stats.py` (bootstrap 95% CIs — headline +19% becomes "+19% point, [+15%, +24%] CI"), `hl_strategies.py` (funding-momentum variant — lost to level by 9pp, useful "what didn't work" data). +9 tests (**54 total**).
- **Day 5 (wow pass — parallel agent work).** Five more concurrent streams to close the "structural ceiling" gap from REDTEAM §6: `microstructure.py` + `MICROSTRUCTURE.md` (the headline empirical finding — 63% trap rate on detector flags, 85% in US state races), `walkforward.py` (out-of-sample validation — OOS slightly beats IS, signal is durable), `hl_stats_block.py` (block bootstrap — autocorrelation widens CI by 28%, honest band is [+14.1%, +25.2%]), `EXECUTION.md` + `scripts/size_basket_trade.py` (real-trade runway with UK-jurisdiction simulation path), `GINA_ENGAGEMENT.md` (product-engagement research — Polymarket not publicly confirmed shipped at Gina, recipes reframed). +23 tests (**79 total**).
- **Day 5 (sharpen pass — parallel agent work).** Four more streams. The single biggest narrative shift here is the volume-weighted reframing: the count-based 55.6% trap rate becomes **0.012% by dollar-weighted volume** because the World Cup `real` event carries 95.9% of flagged dollars. Also: `trap_classifier.py` (LOOCV AUC 0.600 on n=18, scaffolding-grade with documented small-n caveat), `hl_tail.py` (VaR, Expected Shortfall, drawdown duration — the GROSS-vs-NET tail asymmetry is the single most damning statistic against the 8h cadence), and a full visual polish on `dashboard.py` + `plots.py` (monospace numerics, restrained palette, 144 DPI charts, mobile-responsive). +30 tests (**109 total**).

## What would be next

- Polymarket: account for `negRiskOther` and `negRiskAugmented` shifts in the sum constraint; pull `/order-book` for each market in a flagged event to measure executable depth at gap; long-window monitor run for real persistence numbers.
- Hyperliquid: pair funding capture with a real spot/perp hedge model; pull spot prices and compute realized hedge P&L per period.
- Cross-venue: pair Polymarket binary outcomes that are statistically linked to onchain assets (regulatory-decision markets vs BTC funding skew) and test for joint mispricings.
