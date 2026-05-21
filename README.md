# polymarket-edge

Event-level no-arb scanner for Polymarket mutually-exclusive (`negRisk`) markets.

## Thesis

Polymarket enforces `P(YES) + P(NO) = $1` per market via order-book mirroring — every buy of YES at price *p* is simultaneously visible as a sell of NO at `1 - p`. As a result, intra-market arbs are competed out in steady state.

The non-trivial pricing signal lives at the **event level**. For a `negRisk` event with N mutually-exclusive markets, the sum of YES probabilities across the event must equal 1.0 in a fair market. Deviations imply a tradeable arb:

- `sum(best_bid_yes) > 1 + fees`: **sell-side arb** — sell YES across all markets, settle one at $1.00, profit the spread.
- `sum(best_ask_yes) < 1 - fees`: **buy-side arb** — buy YES across all markets, one settles at $1.00, profit the spread.

## Status

Day 1 of a 5-day build. Live scope:

- Pull active events + markets from the Polymarket gamma API
- Persist events / markets / snapshots / signals to SQLite
- Score every active `negRisk` event for both arb directions
- Flag signals that exceed a configurable fee buffer

Days 2–5: historical backtest, Hyperliquid funding-capture scanner, live paper-trading, research note.

## Setup

```
uv sync
uv run polymarket-edge ingest          # pull + persist active events
uv run polymarket-edge scan            # score + flag negRisk events
uv run polymarket-edge stats           # row counts
uv run pytest                          # unit tests
```

## Limitations (read before trusting any number)

- The arb math assumes mutually exclusive AND fully exhaustive coverage within a `negRisk` event. Some events have a `negRiskOther` market representing residual outcomes; the detector flags this case but does not adjust the sum constraint for it yet.
- Quote-fill assumption is `best_bid` / `best_ask`. Real fills cross the book and move price — small arbs (<2%) are unlikely to be executable after Polymarket taker fees.
- Look-ahead bias is absent here because the scanner only scores current snapshots; historical backtest comes day 2 and will need careful point-in-time reconstruction.
- Rate limit on the gamma API is ~60 req/min unauth; ingestion paginates with a 1.2s cooldown to stay well under.
