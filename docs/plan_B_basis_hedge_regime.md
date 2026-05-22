# Plan B — Hyperliquid basis-hedge model + regime conditioning

## Goal

Replace the parametric 5 bps/leg cost in `hl_hedge.py` with a real spot/perp
basis-hedge model, and report regime-conditional net Sharpe so we know
*when* the funding strategy works rather than just *whether* it works on
average.

Headline if positive:

> "After modeling the actual basis hedge against Hyperliquid spot, the
> trailing-K funding capture returns +X% net annualized in the bottom-tercile
> BTC realized vol regime (N=Y rebalances, 95% CI [+Z, +W]). The headline 8h
> cadence is dead globally but works in the low-vol regime."

If negative: closes the REDTEAM §6 / §7 caveat ("hedge cost modeled
parametrically, not from data") — defensible "I extended this rigorously,
here is the honest answer."

## Why this matters

The single biggest credibility gap in the existing Hyperliquid backtest is
the hedge cost being a number I typed (5 bps/leg), not a number derived from
data. A founder reads `hl_hedge.py` and asks "where did 5 come from?" — the
defensible answer is to model the actual hedge.

Regime conditioning is the standard senior-quant follow-on: most funding
strategies work in low-vol regimes and die in high-vol regimes. Showing that
explicitly is the difference between "I ran a backtest" and "I characterized
when the strategy is valid."

## Data dependencies

### Hyperliquid perp candles

Endpoint: `POST https://api.hyperliquid.xyz/info`

Body for spot candles:

```json
{
  "type": "candleSnapshot",
  "req": {
    "coin": "BTC",
    "interval": "1h",
    "startTime": <unix_ms>,
    "endTime": <unix_ms>
  }
}
```

Returns array of candle dicts. Fields: `t` (unix ms), `T` (close time ms),
`s` (coin), `i` (interval), `o` (open), `h` (high), `l` (low), `c` (close),
`v` (volume), `n` (n trades).

Already used in `cross_venue.fetch_hl_mark_history` — reuse / refactor that
function if needed. The HL endpoint for perp uses `coin=BTC`; for spot the
naming convention is `coin=@<index>` or coin name with `@` prefix —
**probe the spot endpoint first** with a known-spot coin (BTC, ETH, SOL)
before building the bulk fetcher.

### Hyperliquid spot listings

Endpoint: `POST https://api.hyperliquid.xyz/info` with `{"type": "spotMeta"}`.
Returns the list of spot pairs. Use this to determine which of the 37 universe
coins (in `hl_universe`) have spot listings. **Coins without spot are
excluded from the hedged backtest** — they remain available for unhedged
sensitivity comparison but are NOT in the headline.

### Funding history

Already in `hl_funding_history`. Use `hl_backtest.load_funding`.

## Module structure

New file: `src/polymarket_edge/hl_basis_hedge.py`

```python
@dataclass(frozen=True, slots=True)
class HedgedTick:
    """One hourly observation per coin: funding + perp mark + spot mark."""
    coin: str
    t_ms: int
    funding: float
    perp_mark: float
    spot_mark: float


@dataclass(frozen=True, slots=True)
class HedgedRebalanceResult:
    """One rebalance: short K perps, long K spots, hold for rebalance_hours."""
    t_ms_open: int
    t_ms_close: int
    coins_held: list[str]
    funding_received: float           # sum of funding over the hold per dollar of notional
    perp_pnl: float                   # short side: positive when perp falls
    spot_pnl: float                   # long side: positive when spot rises
    basis_pnl: float                  # perp_pnl + spot_pnl, what survives hedging
    entry_spread_bps: float           # round-trip entry across the 2 legs, basis points
    exit_spread_bps: float            # round-trip exit
    net_return: float                 # funding_received + basis_pnl - spreads_round_trip


@dataclass(frozen=True, slots=True)
class HedgedBacktestResult:
    n_rebalances: int
    coins_eligible: list[str]
    coins_excluded_no_spot: list[str]
    rebalances: list[HedgedRebalanceResult]
    total_net_return: float
    annualized_net_return: float
    sharpe: float
    max_drawdown: float
    hit_rate: float


@dataclass(frozen=True, slots=True)
class Regime:
    """Volatility regime classification, by trailing BTC realized vol."""
    name: str                # 'low' | 'med' | 'high'
    btc_realized_vol_trailing_7d: float


@dataclass(frozen=True, slots=True)
class RegimeConditionalResult:
    regime: Regime
    n_rebalances: int
    annualized_net_return: float
    sharpe: float
    sharpe_ci_low: float     # bootstrap CI
    sharpe_ci_high: float
    max_drawdown: float


async def fetch_perp_and_spot_candles(
    coins: list[str],
    *,
    days: int = 30,
) -> dict[str, list[dict]]:
    """Pull both perp and spot 1h candles for each eligible coin.
    Returns {coin: {'perp': [...], 'spot': [...]}}. Coins without spot
    return {'perp': [...], 'spot': []}.
    """


def detect_spot_listings(coins: list[str]) -> tuple[list[str], list[str]]:
    """Hit spotMeta and partition the input into (have_spot, no_spot)."""


def merge_to_hedged_ticks(
    funding_rows: list[FundingTick],
    candles: dict[str, dict[str, list[dict]]],
) -> list[HedgedTick]:
    """Join funding + perp_mark + spot_mark by (coin, hour).
    Skip hours where any of the three is missing."""


def backtest_hedged_top_k_trailing(
    ticks: list[HedgedTick],
    *,
    top_k: int = 5,
    trailing_hours: int = 24,
    rebalance_hours: int = 8,
    entry_spread_bps_per_leg: float | None = None,
) -> HedgedBacktestResult:
    """Same selection logic as backtest_top_k_trailing in hl_backtest, but
    P&L includes real basis_pnl and (optionally) a configurable entry/exit
    spread per leg. When spread_bps is None, no extra spread is charged
    beyond what the basis already absorbs.
    """


def classify_regimes(
    btc_perp_candles: list[dict],
    *,
    vol_window_hours: int = 168,
) -> dict[int, Regime]:
    """For each hour t in the BTC series, compute the trailing 168h realized
    log-return vol, and assign a tercile bucket (low/med/high) based on the
    cross-time distribution. Return {t_ms: Regime}."""


def regime_conditional_results(
    result: HedgedBacktestResult,
    regimes: dict[int, Regime],
    *,
    n_bootstrap: int = 2000,
) -> list[RegimeConditionalResult]:
    """Bucket each rebalance by the regime at its open time, compute
    per-regime stats with bootstrap CI on Sharpe."""
```

New script: `scripts/hl_basis_regime.py`

Runnable. Loads funding from DB, fetches perp + spot candles, runs the
hedged backtest, classifies regimes, prints the per-regime table.

New file: `tests/test_hl_basis_hedge.py`

Tests below.

Optional appendix to `REDTEAM.md` — I'll do this myself, do not edit.

## Methodology in detail

### Basis P&L per rebalance

At open time t (rebalance start):
- Enter SHORT 1 unit of perp at `perp_mark[t]` (proceeds go to collateral)
- Enter LONG 1 unit of spot at `spot_mark[t]`
- Pay `entry_spread_bps_per_leg * 2` bps round-trip if the spread arg is set

Over the holding period [t, t + rebalance_hours]:
- Receive funding at each hourly tick: `sum(funding[t+1..t+H]) * notional`
  (only on the perp side; spot doesn't pay funding)

At close time t + H:
- Close SHORT perp at `perp_mark[t+H]` → realized perp_pnl = `perp_mark[t] - perp_mark[t+H]`
  (positive when perp fell, since we were short)
- Close LONG spot at `spot_mark[t+H]` → realized spot_pnl = `spot_mark[t+H] - spot_mark[t]`
  (positive when spot rose)
- Pay `entry_spread_bps_per_leg * 2` bps round-trip again

`basis_pnl = perp_pnl + spot_pnl` — this is zero when basis is stable
(perp and spot move together), positive when basis narrows (perp falls
faster than spot), negative when basis widens.

`net_return = funding_received + basis_pnl - spread_cost`

This is per-unit-notional. Aggregate equal-weighted across coins held in
the rebalance.

### Regime classification

For BTC perp:
- Compute hourly log returns from `c[t]` to `c[t+1]`
- Trailing 168h: realized vol = sqrt(168 * mean(r^2))
- Annualized: realized_vol * sqrt(8760 / 168) — but for regime classification
  we don't need annualizing, just relative ordering
- Across all hours in the dataset, compute the 33rd and 67th percentiles of
  trailing vol
- Each hour is classified low/med/high based on which tercile its trailing
  vol falls in

### Honest reporting of N per regime

22 days × 24 hours = 528 hours. Rebalance every 8h → 66 rebalances. Split
into 3 regimes → ~22 rebalances per regime. **This is small. CI on Sharpe
at N=22 is huge.** Mitigation: report N explicitly, use bootstrap CIs (not
parametric), be explicit in the writeup that the regime claim is directional
not statistical.

## Pre-mortem — top 3 risks

1. **Only ~12 of 37 universe coins have spot (P~70%).** The strategy
   universe shrinks. Mitigation: separately report (a) hedged strategy on
   coins-with-spot, (b) unhedged strategy on all coins, (c) the difference.
   The hedged result is the headline, the unhedged is the sensitivity bound.

2. **N=22 per regime makes Sharpe CIs uninformative (P~90%).** Mitigation:
   - Report explicit N per regime in the headline
   - Use bootstrap CIs (already implemented in `hl_stats.py`)
   - Frame the result as "directional finding pending more data"
   - If you want to push further: also report regime-conditional results
     using a sliding window with overlap (more samples but correlated)

3. **Basis is so stable that perp_pnl + spot_pnl ≈ 0 always (P~50%).** If
   spot tracks perp 1:1, the hedge is "free" and the result essentially
   reproduces the funding-only result without 5bp/leg subtracted. Net result
   might be modestly positive in low-vol regimes for that reason alone.
   Mitigation: this isn't really a failure mode — it's actually a positive
   finding if true, because it means the strategy IS viable when hedged
   real-time. State the basis behavior explicitly in the writeup.

## Tests required

```python
def test_merge_drops_hours_missing_a_leg(): ...
def test_backtest_with_constant_perp_and_spot_equals_funding_only(): ...
def test_backtest_with_zero_funding_returns_basis_pnl_only(): ...
def test_spread_cost_subtracted_exactly_4_legs(): ...
def test_classify_regimes_produces_three_buckets(): ...
def test_classify_regimes_low_vol_period_classified_low(): ...
def test_regime_conditional_bootstrap_ci_wider_at_low_n(): ...
def test_eligible_coins_excludes_no_spot(): ...
```

## Live deliverable — what `scripts/hl_basis_regime.py` must print

```
universe: 37 coins
  with spot:    [list of N coins]
  perp-only:    [list of K coins, excluded from hedged backtest]
data window: 2026-05-01T... -> 2026-05-22T...

UNHEDGED baseline (all 37 coins, funding-only, no spread):
  ann_ret +X.XX%  sharpe +Y.YY  n_rebalances Z

HEDGED (coins with spot, basis modeled, no extra spread):
  ann_ret +X.XX%  sharpe +Y.YY  n_rebalances Z
  basis pnl contribution: +X.XX pp annualized

Regime-conditional (HEDGED + no extra spread):
  regime  n  ann_ret    sharpe   sharpe_95CI       max_dd
  low     22 +X.XX%    +Y.YY    [+A.AA, +B.BB]    C.CC%
  med     22 +X.XX%    +Y.YY    [+A.AA, +B.BB]    C.CC%
  high    22 +X.XX%    +Y.YY    [+A.AA, +B.BB]    C.CC%

With 5 bps/leg spread (4 legs round trip, 20 bps per rebalance):
  (same table)

Best surviving regime under 5 bps/leg: [name] with ann_ret +X.XX%, sharpe +Y.YY
```

## Constraints

- Do NOT modify `hl_backtest.py`, `hl_hedge.py`, `hl_stats.py`, `cli.py`,
  `report.py`, `README.md`, `REDTEAM.md`
- Pure additions: new module + new tests + new script
- Stdlib + existing project deps (`httpx`, `matplotlib` if needed for any
  diagnostic chart — but no chart is required)
- Do NOT commit
- Type hints, 100-char line limit, ruff clean
- Reuse types from `hl_backtest.FundingTick` and helpers from `hl_stats`
  where possible

## Report when done (under 350 words)

1. Test count added, pytest + ruff status
2. **Coin coverage**: of the 37 universe, how many had spot?
3. **The HEDGED result** without extra spread: ann return, Sharpe, N
4. **The best surviving regime** under 5 bps/leg spread: ann return, Sharpe,
   N — this is the headline if positive
5. Basis P&L contribution: how much did the hedge add or subtract from the
   funding-only baseline?
6. Any spec issue you pushed back on (e.g., a coin's spot listing was so
   illiquid the basis P&L was dominated by ticks, etc.)
7. Honest interpretation: is the regime-conditional result credible at this N?
