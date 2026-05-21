# polymarket-edge

[![ci](https://github.com/harrywinter06-code/polymarket-edge/actions/workflows/ci.yml/badge.svg)](https://github.com/harrywinter06-code/polymarket-edge/actions/workflows/ci.yml)

Event-level no-arb scanner for Polymarket mutually-exclusive (`negRisk`) markets, plus a Hyperliquid funding-capture backtest. Built in five days as ammunition for an Ask Gina quant-intern application.

A red-team self-audit of every claim below lives in [REDTEAM.md](REDTEAM.md). Read that file before trusting any number. The product framing for Ask Gina specifically — what shippable recipes follow from these findings — lives in [RECIPES.md](RECIPES.md). A cross-venue case study (Fed-rate-cuts Polymarket market vs BTC perp funding, null result, rigorous method) lives in [CROSSVENUE.md](CROSSVENUE.md). A single-file [`dashboard.html`](dashboard.html) renders the headline numbers and charts in a browser without external dependencies.

## What it does

**Polymarket leg.** `P(YES) + P(NO) = $1` is contract-enforced per market via the CLOB order-mirroring rule — every buy of YES at price *p* is simultaneously visible as a sell of NO at `1 - p`. Intra-market arbs are competed out in steady state, so the non-trivial signal lives at the **event** level. For a `negRisk` event with *N* mutually-exclusive markets, the sum of YES probabilities across the event must equal 1.0 in fair pricing. Deviations imply tradeable arb:

- `sum(best_bid_yes) > 1`: **sell-side** — sell YES across all markets; exactly one settles at $1.
- `sum(best_ask_yes) < 1`: **buy-side** — buy YES across all markets; exactly one settles at $1.

The scanner ingests every active event from the gamma API, scores every `negRisk` event, and flags deviations exceeding a configurable fee buffer. A forward-observation `monitor` records signal trajectories; the `persistence` and `forward-test` analyses measure how quickly flagged signals decay toward fair.

**Hyperliquid leg.** The info endpoint exposes hourly funding per perpetual and an unconstrained historical series via `fundingHistory`. The backtest tests a top-K trailing-window funding-capture strategy: at each rebalance tick, rank coins by trailing-mean funding, short the top K (equal-weight), realize the actual funding flow over the next interval. Benchmarks: perfect-hindsight (look-ahead top-K) and passive-short on a chosen coin.

## Results (with sensitivity)

**Polymarket — live snapshot, depth-aware (build window).** Across 100 active events / 1,440 markets / 18 `negRisk` events scored, three real microstructure deviations at the 50bp threshold — but **only one of them actually trades**:

| event | n_mkts | top-of-book gap | gap at $1K/mkt | tradeable? |
|---|---|---|---|---|
| 2026 FIFA World Cup Winner | 48 | +150bp sell | **+150bp** | **YES** — $48K basket, $145K max before Iran throttles |
| 2028 US Election party | 2 | +100bp buy | +50bp, **inverts at $5K** | marginal — small size only |
| Harvey Weinstein sentencing | 6 | +80bp sell | **−1,040bp at $50/mkt** | **TRAP** — one market has $7.83 total bid depth |

This is the core finding of the project. A top-of-book gap detector flags all three. A *depth-aware* basket model — `book_depth.py`, which walks each market's full `/book` and computes the basket-trade average fill — separates the real signal (World Cup, executable at meaningful size and clearing the 0.75% Sports taker fee) from the marginal one (Election, fee-clearable only at retail size) from the trap (Weinstein, where the naïve top-of-book reading would lose money instantly).

**Hyperliquid — GROSS backtest sensitivity (30d, 18,500 hourly ticks, 38 perps).**

| top_K | trail | rebal | n | annualized | Sharpe | hit% |
|---|---|---|---|---|---|---|
| 3 | 24h | 8h | 56 | **+21.5%** | +28.7 | 92.9% |
| 5 | 24h | 8h | 56 | +19.0% | +37.0 | 98.2% |
| 10 | 24h | 8h | 56 | +14.9% | +49.5 | 98.2% |
| perfect-hindsight K=5 | — | 8h | 59 | +22.3% | +39.5 | 100.0% |
| passive short BTC | — | 8h | 62 | +2.3% | +13.4 | 66.1% |

Gross decomposition of the +19.0% top-5: ~11.0% comes from the base-rate funding floor (interest-rate component, ~10.95% APR — any coin near zero premium pays shorts this passively). The remaining ~8.0 percentage points are the selection excess from the trailing-mean predictor. The trailing-24h variant recovers ~85% of the perfect-hindsight K=5 ceiling.

**Bootstrap 95% CI on the headline (5000 resamples, N=56 rebalances):**
- Annualized return: **+19.03% [point], 95% CI [+14.88%, +23.69%]**
- Sharpe: +36.98 [point], 95% CI [+30.24, +52.83] — wide and right-skewed (small-N artifact)

The lower bound on annualized return is +14.9%, not +19% — the founder-defensible claim. Run via `polymarket-edge hl-ci`.

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
- **No historical Polymarket backtest.** CLOB `/prices-history` floors at 12h granularity for resolved markets ([py-clob-client#216](https://github.com/Polymarket/py-clob-client/issues/216)), so an execution-grade historical backtest is infeasible. The forward-observation persistence study fills the gap (and was supposed to run for hours during the build; ran into a host-side virtual-memory limit — see REDTEAM.md item 2d).
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
uv run polymarket-edge paper-auto      # one round of paper-trading
uv run polymarket-edge hl-ci           # bootstrap 95% CIs on Sharpe / ann return
uv run polymarket-edge report          # write REPORT.md (+ chart PNGs)
uv run polymarket-edge dashboard       # write dashboard.html (single-file, embeds charts)
$env:PYTHONPATH="src"; python scripts/cross_venue_case.py  # cross-venue case study
uv run pytest                          # 54 tests
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

## What would be next

- Polymarket: account for `negRiskOther` and `negRiskAugmented` shifts in the sum constraint; pull `/order-book` for each market in a flagged event to measure executable depth at gap; long-window monitor run for real persistence numbers.
- Hyperliquid: pair funding capture with a real spot/perp hedge model; pull spot prices and compute realized hedge P&L per period.
- Cross-venue: pair Polymarket binary outcomes that are statistically linked to onchain assets (regulatory-decision markets vs BTC funding skew) and test for joint mispricings.
