# Plan D — Funding extreme directional study

## Goal

Quantify whether perp prices at extreme funding events (|z| > 2 vs trailing
168h) rally or crash over the next 24h. Specifically: at extreme POSITIVE
funding (longs paying through the nose), do prices subsequently grind down
(confirming the standard "top-K-funding-short" intuition) or do they rally
(contrarian — shorts get squeezed)?

The standard top-K-funding-short backtest already in the project assumes the
former. This plan tests directly. The deliverable is a clean directional
signal:

> "At z > 2 positive funding extremes (N=X events across 37 coins / 22 days),
> the perp returns +Y bps net of funding paid over the next 24h on average,
> with Sharpe Z. The contrarian LONG side is the actual edge."

Or the opposite:

> "At z > 2 extremes, perps fall an average of Y bps over 24h, confirming
> the short-side thesis with a sharper signal than the trailing-K strategy."

Either result is a finding. Both are tradeable.

## Why this matters

The existing trailing-K strategy averages over all positive-funding coins.
At extremes specifically, the signal could be qualitatively different — either
because shorts are crowded (long squeeze coming) or because longs are about
to capitulate (price collapse imminent). The existing data has never been
sliced at the *tail* of the funding distribution. This is the cleanest "I
looked somewhere nobody else looked" finding available without new data.

## Data dependencies

### Hyperliquid funding history

Already in DB. Use `hl_backtest.load_funding`. 22 days × 37 coins × ~hourly =
~19,500 rows.

### Hyperliquid perp prices

NEW. Need hourly perp close prices for each coin. Endpoint:

```json
POST https://api.hyperliquid.xyz/info
{
  "type": "candleSnapshot",
  "req": {
    "coin": "BTC",
    "interval": "1h",
    "startTime": <unix_ms_22d_ago>,
    "endTime": <unix_ms_now>
  }
}
```

Returns array of candles with `c` (close) and `t` (timestamp). Persist to a
new SQLite table `hl_perp_candles` (schema add to `schema.sql`).

The existing `cross_venue.fetch_hl_mark_history` already fetches HL candles
for BTC. Generalize that helper, or duplicate the pattern for the universe.

### NOTE: data overlap with Plan B

Plan B (basis hedge) also pulls HL perp candles. To avoid double work:

**Option 1 (clean separation, recommended):** This plan pulls perp prices
independently into `hl_perp_candles`. Plan B does its own pulls. They're
independent runs.

**Option 2 (shared):** Coordinate with Plan B to write a single shared
fetcher. More efficient but introduces a sequencing dependency.

Go with Option 1 unless Plan B has already shipped at the time you start.

## Module structure

New file: `src/polymarket_edge/hl_extremes.py`

```python
@dataclass(frozen=True, slots=True)
class FundingPriceObservation:
    coin: str
    t_ms: int
    funding: float
    perp_close: float
    trailing_mean: float
    trailing_std: float
    z_score: float


@dataclass(frozen=True, slots=True)
class ExtremeEventResult:
    """One extreme event: entry at t, exit at t + hold_hours."""
    coin: str
    entry_t_ms: int
    entry_z: float
    entry_funding: float
    entry_price: float
    exit_t_ms: int
    exit_price: float
    price_return: float           # exit/entry - 1
    funding_paid_long: float      # sum of funding over hold (long pays positive)
    long_net_return: float        # price_return - funding_paid_long
    short_net_return: float       # -price_return + funding_paid_long
    hold_hours: int


@dataclass(frozen=True, slots=True)
class ExtremeStudyResult:
    z_threshold: float
    direction: str                 # 'positive' (z > thresh) or 'negative' (z < -thresh)
    hold_hours: int
    n_events: int
    n_coins: int
    mean_price_return: float
    mean_funding_paid_long: float
    mean_long_net_return: float
    mean_short_net_return: float
    long_sharpe: float             # mean / std across events, scaled
    short_sharpe: float
    long_t_stat: float
    short_t_stat: float
    long_hit_rate: float
    short_hit_rate: float
    events: list[ExtremeEventResult]


async def fetch_perp_candles_for_universe(
    coins: list[str],
    *,
    days: int = 22,
    output_table: str = "hl_perp_candles",
) -> int:
    """Pull hourly perp candles for each coin, persist to SQLite.
    Returns number of rows written."""


def merge_funding_and_prices(
    funding_rows: list[FundingTick],
    candles_by_coin: dict[str, list[dict]],
) -> list[FundingPriceObservation]:
    """Join funding + perp_close by (coin, hour) and pre-compute the
    trailing 168h mean/std/z-score per row.

    Rows in the first 168 hours per coin are dropped (no trailing window
    available)."""


def identify_extreme_events(
    obs: list[FundingPriceObservation],
    *,
    z_threshold: float = 2.0,
    direction: str = "positive",
) -> list[FundingPriceObservation]:
    """Filter to rows where the z-score crosses the threshold in the
    specified direction.

    De-dupe consecutive extremes per coin within `cooldown_hours` to avoid
    counting the same regime multiple times — TODO during implementation,
    parameterize this. Start with no cooldown and document the implication."""


def hold_to_exit(
    entries: list[FundingPriceObservation],
    obs_by_coin: dict[str, list[FundingPriceObservation]],
    *,
    hold_hours: int = 24,
) -> list[ExtremeEventResult]:
    """For each entry, find the price `hold_hours` later and compute the
    return path. Drop entries where the exit row is missing (end of data)."""


def summarize(
    events: list[ExtremeEventResult],
    *,
    z_threshold: float,
    direction: str,
    hold_hours: int,
) -> ExtremeStudyResult:
    """Aggregate per-event results into the study-level summary, including
    long-side and short-side net returns."""
```

New script: `scripts/hl_extremes_study.py`

Loads funding from DB, fetches perp candles, merges, identifies extreme
events at multiple thresholds, runs the study at multiple hold horizons,
prints results.

New file: `tests/test_hl_extremes.py`

Tests below.

## Methodology in detail

### Z-score computation

For each `(coin c, hour t)`:

```
window = funding[c, t-167 : t]                # 168 trailing hours, not including t
mean = mean(window)
std = pstdev(window)
z = (funding[c, t] - mean) / std   if std > 0 else NaN
```

Drop rows where `std == 0` (constant trailing window) — can happen for coins
at the base-rate floor.

### Extreme event identification

`z > z_threshold` (positive) or `z < -z_threshold` (negative). Default
threshold = 2.0 (~5% of observations if z is approximately normal — sanity-
check with a histogram).

### Hold-to-exit P&L

At entry time `t_in` and exit time `t_out = t_in + hold_hours`:

- `price_return = perp_close[t_out] / perp_close[t_in] - 1`
- `funding_paid_long = sum(funding[t_in + 1 : t_out + 1])`
- `long_net_return = price_return - funding_paid_long`
- `short_net_return = -price_return + funding_paid_long`

Per-unit-notional. Aggregate equal-weighted across events.

### Statistics

- `mean_long_net_return`: arithmetic mean of `long_net_return` across events
- `long_sharpe`: `(mean_long_net_return * sqrt(periods_per_year)) / std(long_net_return)`
  — but careful with sqrt scaling at sub-period horizons. For 24h hold:
  `periods_per_year = 365`. Document the convention.
- `long_t_stat`: `mean / (std / sqrt(N))` — assumes events are independent,
  which is approximately true if `cooldown_hours >= hold_hours`
- `hit_rate`: fraction of events with positive net return on the long side

### Multiple-hypothesis correction

This plan tests:
- z_threshold ∈ {1.5, 2.0, 2.5} → 3 thresholds
- direction ∈ {positive, negative} → 2 directions
- hold_hours ∈ {6, 24, 72} → 3 horizons

That's 18 sub-tests. **Bonferroni-correct any headline claim** — require
t-stat > 3.05 (single-test α=0.05 / 18) before claiming an effect "survives."

Document this explicitly in the writeup.

## Pre-mortem — top 3 risks

1. **N too small at z>2 (P~30%).** Of 19,500 funding observations, ~5% are
   z>2 → ~975 events across 37 coins. After dropping the first 168h per coin
   (no trailing window): 22d × 24h - 168h = 360h per coin × 37 coins ≈ 13,320
   eligible obs → ~666 events. **Probably enough.** If actual N < 100,
   document and run at z=1.5 instead.

2. **Coin-clustering effect (P~50%).** If two coins are highly correlated
   (e.g., two memecoins on the same meme cycle), a single market event could
   produce extremes on both at the same hour, double-counting the same
   underlying event. Mitigation: report results separately on the 5–10 most
   liquid coins (deeper books, more independent) and on the full universe.
   If the conclusions differ, the liquid-only result is the headline.

3. **Selection bias from end-of-window truncation (P~10%).** Events in the
   last `hold_hours` of the data window can't compute exit returns. Drop them
   cleanly; document the implication.

## Tests required

```python
def test_z_score_on_constant_series_returns_nan(): ...
def test_z_score_matches_manual_calc(): ...
def test_extreme_event_identification_thresholds(): ...
def test_hold_to_exit_drops_truncated_entries(): ...
def test_long_net_return_subtracts_funding_paid(): ...
def test_short_net_return_signs_inverse_of_long(): ...
def test_summarize_handles_zero_events(): ...
def test_sharpe_annualization_convention_documented(): ...
```

## Live deliverable — what `scripts/hl_extremes_study.py` must print

```
universe: 37 coins, 22 days, ~19,500 funding observations
eligible after dropping first 168h per coin: 13,320

POSITIVE extremes (z > 2.0), 24h hold:
  n_events: X
  n_coins: Y
  mean price_return: +X.XX%  (std Y.YY%)
  mean funding paid over 24h: +Z.ZZ%
  mean LONG net return: +A.AA%   sharpe +B.BB  t-stat +C.CC  hit_rate D.DD%
  mean SHORT net return: -A.AA%  sharpe -B.BB  t-stat -C.CC  hit_rate E.EE%

POSITIVE extremes (z > 1.5), 24h hold: (sample size grows)
  ...
POSITIVE extremes (z > 2.5), 24h hold: (sample size shrinks)
  ...

NEGATIVE extremes (z < -2.0), 24h hold:
  ...

Sensitivity to hold horizon (z > 2.0, positive):
  6h  hold: long_net +X.XX% sharpe +Y.YY t-stat +Z.ZZ
  24h hold: ...
  72h hold: ...

Bonferroni-corrected significance: t-stat > 3.05 required for the 18-test family.
Surviving claims:
  - [list of (threshold, direction, horizon) triples that clear the bar]
or
  - None clear Bonferroni; the closest is [...] with t-stat [...].

Per-coin breakdown (liquid universe only — BTC, ETH, SOL, XRP, DOGE):
  coin     n_extreme_events   mean_long_net   sharpe
  ...

Saved to results/hl_extremes_<timestamp>.json
```

## Writeup — append section to `MICROSTRUCTURE.md`

Title the section `## Hyperliquid funding extremes — directional study`.
~400-600 words. Use the live numbers. Lead with the headline number on the
side that won (long or short), with the t-stat and Bonferroni adjustment.
Caveats section covers the cooldown / clustering / N issues. Closing sentence
connects to Ask Gina: "the trade is 24h-hold-from-extreme; sized via the
existing depth analysis pattern."

## Constraints

- Do NOT modify `hl_backtest.py`, `hl_hedge.py`, `hl_stats.py`, `cli.py`,
  `report.py`, `README.md`, `REDTEAM.md`
- Schema add (one new table `hl_perp_candles`) is allowed
- Do NOT commit
- Stdlib only (no scipy, no statsmodels — implement t-stat and Sharpe inline)
- Type hints throughout
- 100-char line limit, ruff clean
- Match the existing module style

## Report when done (under 350 words)

1. Test count added, pytest + ruff status
2. **The headline number** — at z > 2 positive extremes, 24h hold, what was
   the mean long-side net return and the t-stat?
3. Same for short-side: did the standard "high-funding-shorts-win" intuition
   hold at extremes?
4. After Bonferroni: which sub-tests survive?
5. Per-coin: did liquid-universe coins give the same conclusion as the full
   universe?
6. Honest interpretation: is there a real directional edge at funding
   extremes, or is the result null?
