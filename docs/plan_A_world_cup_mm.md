# Plan A — World Cup market-maker simulation

## Goal

A defensible projected P&L for a maker-side strategy on the 2026 FIFA World Cup
negRisk basket, expressed as:

> "Deploying $K of maker capital across the 48 constituent markets yields
> ~$M/day net of the 0.75% Sports taker fee, the 20–25% maker rebate, and
> realistic adverse selection, projected over the ~50 days to the tournament
> finals. Tested under three adverse-selection assumptions."

Success means a clean *positive* number under at least the moderate AS scenario,
with the methodology defensible enough that a quant founder can probe it without
tripping a hidden assumption.

If the number is negative or breakeven, that is also a publishable finding —
"the maker rebate exists but is consumed by adverse selection on this specific
event class" — but the email-leading scenario requires positive.

## Why this matters

The project so far identifies signals but has never *priced* a real strategy
that captures one. The World Cup is the one negRisk event in the dataset that
clears the depth filter at meaningful size. A market-making sim, projected
forward, converts "I built a scanner" into "I quantified the maker yield on a
specific live event."

## Data dependencies

### Polymarket CLOB `/trades`

Endpoint: `https://clob.polymarket.com/trades?market=<token_id>`

Verify schema with a smoke call before building the simulator. Expected fields
per trade (best-guess from prior search results — confirm):

- `id` or similar (trade ID)
- `market` (token ID)
- `side` (taker side, BUY/SELL)
- `price` (decimal in [0, 1])
- `size` (shares)
- `timestamp` or `t` (unix seconds or ms — verify)
- `taker_order_id`
- `maker_order_id`

Pagination is likely required for high-volume markets (Brazil, Spain, etc.).
Use the cursor or offset scheme the API exposes. **Verify with a probe** before
committing to a pagination strategy.

If the endpoint is auth-gated, fall back to `/last-trade-price` repeatedly over
a polling window — but this only gives you the latest single trade, not history.
Document the limitation in the writeup if you have to fall back.

### Polymarket CLOB `/book`

Already wrapped in `polymarket_edge.book_depth.fetch_books_for_event`. Use the
existing function — do not reimplement. You need current book state for two
things: (1) measuring current spread per market to seed the simulator, (2)
verifying the basket is still flagged and tradeable at simulation time.

### Polymarket gamma `/events`

Already wrapped in `polymarket_edge.fetch.fetch_all_active_events`. Use to pull
the World Cup event metadata (slug: `2026-fifa-world-cup-winner-595`).

## Module structure

New file: `src/polymarket_edge/polymarket_mm_sim.py`

```python
@dataclass(frozen=True, slots=True)
class Trade:
    """One historical trade on a CLOB market."""
    token_id: str
    timestamp_s: int
    price: float
    size_shares: float
    taker_side: str  # 'BUY' or 'SELL'


@dataclass(frozen=True, slots=True)
class AdverseSelectionScenario:
    """How much adverse-selection cost to charge per maker fill, expressed
    as a fraction of the half-spread."""
    name: str           # 'naive' | 'moderate' | 'informed'
    realized_half_spread_fraction: float
    description: str    # one-line explanation


@dataclass(frozen=True, slots=True)
class MarketMMResult:
    token_id: str
    market_question: str
    n_trades_observed: int
    estimated_maker_fills: int       # how many of those a hypothetical sole maker captures
    gross_rebate_usd: float
    adverse_selection_cost_usd: float
    net_pnl_usd: float
    per_day_net_usd: float
    days_observed: float


@dataclass(frozen=True, slots=True)
class EventMMResult:
    event_slug: str
    event_title: str
    scenario: AdverseSelectionScenario
    n_markets_simulated: int
    total_gross_rebate_usd: float
    total_adverse_selection_usd: float
    total_net_pnl_usd: float
    per_day_net_usd: float
    days_observed: float
    projected_pnl_to_resolution_usd: float
    per_market: list[MarketMMResult]


async def fetch_trades_for_token(
    client: httpx.AsyncClient,
    token_id: str,
    *,
    lookback_days: int = 30,
) -> list[Trade]:
    """Fetch historical trades for a single token, paginated."""


def simulate_market_maker(
    trades: list[Trade],
    *,
    scenario: AdverseSelectionScenario,
    maker_rebate_bps_of_notional: float = 18.75,   # 25% of 0.75% Sports taker
    sole_maker_capture_fraction: float = 0.5,       # share of trades the maker is on
) -> MarketMMResult:
    """Run the sim for a single market. Returns the per-market result.

    Logic:
      - We assume the maker is the resting counterparty on a fraction
        `sole_maker_capture_fraction` of the observed historical trades. Tune
        this to model competitive density.
      - For each captured trade: gross rebate = trade_notional * rebate_bps/10000
      - For each captured trade: adverse selection cost = trade_notional *
        observed_half_spread * scenario.realized_half_spread_fraction
      - Sum across all captured trades to get per-market net P&L
      - Annualize / per-day rate based on the observation window
    """


def simulate_basket(
    trades_by_token: dict[str, list[Trade]],
    market_questions: dict[str, str],
    *,
    scenario: AdverseSelectionScenario,
    event_slug: str,
    event_title: str,
    days_to_resolution: float,
    **mm_kwargs: object,
) -> EventMMResult:
    """Aggregate per-market sim into an event-level result with projection."""
```

New script: `scripts/world_cup_mm_sim.py`

Runnable. Pulls live World Cup event from gamma, fetches 30d trade history
for each of 48 constituent markets, runs `simulate_basket` under all three
adverse selection scenarios, prints results. Saves output to
`results/world_cup_mm_<timestamp>.json` (gitignored — add `results/` to
`.gitignore` if not already there).

New file: `tests/test_polymarket_mm_sim.py`

Tests below.

New file: `WORLD_CUP_MM.md`

Writeup with the actual numbers. ~600-800 words.

## Methodology in detail

### Estimating maker fills from trade history

We don't have orderbook snapshots over time, only trade history. The cleanest
approximation: assume the maker is on the inside (best price level) for a
configurable fraction of trades. Default to 0.5 (50% of trades fill against
the maker we're modeling) — this represents a moderately-competitive book with
the maker capturing roughly half the inside flow.

Sensitivity: report results at fractions 0.25, 0.5, 0.75 as well to bound the
estimate.

### Gross rebate per fill

Sports category taker fee is 0.75%. Maker rebate is 25% of that = 0.1875% =
18.75 bps of notional, per fill, paid to the maker. For a $10 fill, the maker
earns $0.01875.

### Adverse selection cost per fill

When a maker is filled, the book typically moves against them. The cost is
parameterized as a fraction of the half-spread observed at trade time. We
don't have order-book snapshots, so estimate the half-spread from the realized
price movement over the next K minutes (K=5 default).

Three scenarios:

| name | realized_half_spread_fraction | meaning |
|---|---|---|
| naive | 0.0 | no adverse selection — upper bound on MM P&L |
| moderate | 0.5 | maker pays half the spread on average (textbook MM literature) |
| informed | 1.0 | maker pays full spread on every fill — pessimistic |

The "true" number is between moderate and informed. Sports markets on
Polymarket have more retail noise than informed flow, so moderate is the
defensible default for the headline number.

### Projecting to tournament resolution

Per-day net P&L observed over 30d → projected over `days_to_resolution`
(~50 days assuming tournament finals in mid-July 2026). Linear projection
with the caveat that flow is likely *higher* approaching the tournament
(matches drive volume), so this projection is conservative. State that in
the writeup.

## Pre-mortem — top 3 risks

1. **/trades endpoint shape or auth blocks data fetch (P~20%).**
   Mitigation: spend the first 30 minutes verifying the endpoint with a probe
   before building. If it requires auth or is rate-limited, fall back to
   inferring trade rate from `volume24hr` per market and modeling trade
   distribution rather than actual fills. Document the fallback in the writeup.

2. **Long-tail markets have <10 trades in 30 days (P~60%).**
   Iran, Switzerland, Korea, etc. will have sparse data. Mitigation: report
   the per-market breakdown — readers see which markets carry the result.
   Aggregate fairly: if 5 markets carry 80% of the rebate and the other 43
   carry 20%, that's the finding. Honesty is better than padding.

3. **Adverse selection is so assumption-dependent the result isn't credible
   (P~50%).** Mitigation: report all three scenarios. Show the breakeven
   half-spread fraction (the AS fraction at which net P&L crosses zero) — this
   is the single most informative number because it tells the reader "you only
   need to believe AS is less than X% of spread for this strategy to be
   positive."

## Tests required

```python
def test_simulator_empty_trades_returns_zero_pnl(): ...
def test_simulator_naive_scenario_equals_pure_rebate(): ...
def test_simulator_informed_scenario_lower_than_naive(): ...
def test_simulator_capture_fraction_scales_linearly(): ...
def test_basket_aggregation_sums_per_market_results(): ...
def test_projection_to_resolution_handles_zero_observed_days(): ...
def test_adverse_selection_charges_match_manual_calculation(): ...
```

Use synthetic Trade lists. No live API in unit tests.

## Live deliverable — what `scripts/world_cup_mm_sim.py` must print

```
event: 2026 FIFA World Cup Winner
  active markets: 48
  observation window: 30 days
  maker rebate: 18.75 bps of notional (Sports, 25% of 0.75% taker fee)

Per-scenario results (sole_maker_capture_fraction=0.5):
  naive       : gross rebate $X  AS $0  net $X  per-day $Y  projected to 50d $Z
  moderate    : gross rebate $X  AS $W  net $V  per-day $U  projected to 50d $T
  informed    : gross rebate $X  AS $W  net $V  per-day $U  projected to 50d $T

Sensitivity to maker capture fraction (moderate AS):
  0.25: per-day $U  projected $T
  0.50: per-day $U  projected $T
  0.75: per-day $U  projected $T

Breakeven half-spread fraction: X.XX
  -> net P&L is zero when AS = X.XX of spread; positive below that

Top 5 markets by net P&L contribution (moderate scenario):
  ...
Bottom 5:
  ...

Saved to results/world_cup_mm_2026-05-22T....json
```

## Writeup — `WORLD_CUP_MM.md`

Sections:

1. The question (one paragraph)
2. Method — including the AS model and the maker-capture approximation
3. Results — the headline number plus all three scenarios plus sensitivity
4. Why it's positive (or not) — mechanical explanation
5. Caveats — the AS modeling, the projection linearity, the trade-data
   approximation
6. Why this matters for Ask Gina — connection to a shippable maker-recipe

## Constraints

- Do NOT modify any existing module in `src/polymarket_edge/` except the new
  `polymarket_mm_sim.py`
- Do NOT modify `cli.py`, `report.py`, `README.md`, `REDTEAM.md` — I'll wire
  the new command and update the docs myself after you return
- Do NOT commit
- No new project dependencies (use existing `httpx`)
- 100-char line limit, ruff clean, type hints everywhere
- Add `results/` to `.gitignore` if you create that directory

## Report when done (under 350 words)

1. Total tests added, pytest + ruff status
2. **The headline net per-day P&L under the moderate scenario, projected to
   50 days** — the single number that matters
3. The breakeven half-spread fraction
4. Which markets carried the result (top contributors)
5. Was the /trades endpoint as expected, or did you have to fall back? What
   did you fall back to?
6. Any methodological choice you made that the spec didn't pin down
7. Honest interpretation: is this credibly positive, credibly negative, or
   "depends on AS assumption"?
