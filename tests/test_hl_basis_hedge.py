"""Tests for hl_basis_hedge.

Covers the eight required behaviours in the plan plus the spot-listing
partitioning. No network calls — spot detection is exercised against a
synthetic ``spotMeta`` payload via the pure helper.
"""

from __future__ import annotations

import math
import random

from polymarket_edge.hl_backtest import FundingTick
from polymarket_edge.hl_basis_hedge import (
    HOUR_MS,
    HedgedBacktestResult,
    HedgedRebalanceResult,
    HedgedTick,
    Regime,
    _build_spot_index_by_base,
    _coin_to_spot_label,
    backtest_hedged_top_k_trailing,
    classify_regimes,
    merge_to_hedged_ticks,
    regime_conditional_results,
)


def _grid(
    coin: str, n_hours: int, *, start_ms: int = 0,
    funding: float = 0.0001, perp: float = 100.0, spot: float = 100.0,
) -> list[HedgedTick]:
    return [
        HedgedTick(
            coin=coin, t_ms=start_ms + i * HOUR_MS,
            funding=funding, perp_mark=perp, spot_mark=spot,
        )
        for i in range(n_hours)
    ]


def _funding_ticks(coin: str, n_hours: int, rate: float, *, start_ms: int = 0,
                   ) -> list[FundingTick]:
    return [FundingTick(coin, start_ms + i * HOUR_MS, rate) for i in range(n_hours)]


def _candles(start_ms: int, n_hours: int, close: float) -> list[dict]:
    return [
        {"t": start_ms + i * HOUR_MS, "c": str(close)} for i in range(n_hours)
    ]


# ---------------------------------------------------------------------------
# merge_to_hedged_ticks
# ---------------------------------------------------------------------------


def test_merge_drops_hours_missing_a_leg() -> None:
    """Any hour without all three of (funding, perp candle, spot candle) is
    dropped — no partial rows leak into the backtest."""
    funding = _funding_ticks("BTC", 5, rate=0.0001)
    perp = _candles(0, 5, 100.0)
    # Spot only covers hours 1..3 (out of 0..4).
    spot = [{"t": HOUR_MS * i, "c": "100.0"} for i in (1, 2, 3)]
    merged = merge_to_hedged_ticks(
        funding, {"BTC": {"perp": perp, "spot": spot}},
    )
    times = sorted(h.t_ms for h in merged)
    assert times == [HOUR_MS, 2 * HOUR_MS, 3 * HOUR_MS]


def test_merge_drops_coin_with_no_spot_candles() -> None:
    """A coin without any spot candles produces zero HedgedTicks even if
    perp + funding are fully populated."""
    funding = _funding_ticks("ETH", 5, rate=0.0002)
    perp = _candles(0, 5, 2000.0)
    merged = merge_to_hedged_ticks(
        funding, {"ETH": {"perp": perp, "spot": []}},
    )
    assert merged == []


# ---------------------------------------------------------------------------
# backtest_hedged_top_k_trailing
# ---------------------------------------------------------------------------


def test_backtest_with_constant_perp_and_spot_equals_funding_only() -> None:
    """If perp and spot are constant and equal, basis_pnl = 0 every rebalance
    and with no extra spread, net_return = funding_received exactly."""
    n_hours = 24 + 8 * 5  # trailing + 5 rebalances
    ticks = _grid("BTC", n_hours, funding=0.0001, perp=100.0, spot=100.0) + \
        _grid("ETH", n_hours, funding=0.00005, perp=2000.0, spot=2000.0)
    result = backtest_hedged_top_k_trailing(
        ticks, top_k=1, trailing_hours=24, rebalance_hours=8,
        entry_spread_bps_per_leg=None,
    )
    assert result.n_rebalances == 5
    for rb in result.rebalances:
        assert rb.basis_pnl == 0.0
        assert rb.net_return == rb.funding_received
    # Funding-only expectation: top-1 picks BTC every rebalance (0.0001 > 0.00005).
    # Each rebalance accrues 8 * 0.0001 = 0.0008.
    for rb in result.rebalances:
        assert rb.net_return == math.pow(10, -4) * 8


def test_backtest_with_zero_funding_returns_basis_pnl_only() -> None:
    """Set funding=0 throughout and inject a known perp drop — net_return must
    equal the basis P&L (perp_pnl + spot_pnl) per rebalance."""
    n_hours = 24 + 8 * 2
    # Open of rebalance 2 = hour 32 (perp=100). Close of rebalance 2 = hour 39.
    # Drop perp to 99 only AT the close hour (39) so the short side profits 1%.
    btc: list[HedgedTick] = []
    for i in range(n_hours):
        t = i * HOUR_MS
        perp = 99.0 if i == 39 else 100.0
        btc.append(HedgedTick("BTC", t, 0.0, perp, 100.0))
    result = backtest_hedged_top_k_trailing(
        btc, top_k=1, trailing_hours=24, rebalance_hours=8,
        entry_spread_bps_per_leg=None,
    )
    assert result.n_rebalances == 2
    for rb in result.rebalances:
        assert rb.funding_received == 0.0
        assert rb.net_return == rb.basis_pnl
    # Rebalance 1: hours 24..31, all 100 -> perp_pnl=0; spot flat -> 0.
    assert result.rebalances[0].basis_pnl == 0.0
    # Rebalance 2: hour 32 open at 100, hour 39 close at 99 -> short perp gains 1%.
    assert math.isclose(result.rebalances[1].basis_pnl, 0.01, rel_tol=1e-12)


def test_spread_cost_subtracted_exactly_4_legs() -> None:
    """With ``entry_spread_bps_per_leg=5`` the per-rebalance cost is 20 bps
    (2 entry legs + 2 exit legs). Net return drops by exactly that amount
    relative to the no-spread baseline, holding everything else equal."""
    n_hours = 24 + 8 * 4
    ticks = _grid("BTC", n_hours, funding=0.0002, perp=100.0, spot=100.0)
    no_spread = backtest_hedged_top_k_trailing(
        ticks, top_k=1, trailing_hours=24, rebalance_hours=8,
        entry_spread_bps_per_leg=None,
    )
    with_spread = backtest_hedged_top_k_trailing(
        ticks, top_k=1, trailing_hours=24, rebalance_hours=8,
        entry_spread_bps_per_leg=5.0,
    )
    assert no_spread.n_rebalances == with_spread.n_rebalances
    expected_cost = 4 * 5.0 / 10_000  # 20 bps
    for a, b in zip(no_spread.rebalances, with_spread.rebalances, strict=True):
        assert math.isclose(a.net_return - b.net_return, expected_cost, rel_tol=1e-12)
        assert math.isclose(
            b.entry_spread_bps + b.exit_spread_bps, 20.0, rel_tol=1e-12,
        )


def test_eligible_coins_excludes_no_spot() -> None:
    """Only coins that survived ``merge_to_hedged_ticks`` (which requires both
    perp and spot candles) appear in the eligible-coins list of the backtest."""
    n_hours = 24 + 8 * 3
    funding = _funding_ticks("BTC", n_hours, 0.0001) + \
        _funding_ticks("ALT", n_hours, 0.0002)
    candles = {
        "BTC": {
            "perp": _candles(0, n_hours, 50_000.0),
            "spot": _candles(0, n_hours, 50_000.0),
        },
        "ALT": {  # perp only, no spot
            "perp": _candles(0, n_hours, 1.0),
            "spot": [],
        },
    }
    hedged = merge_to_hedged_ticks(funding, candles)
    coins_in_hedged = {h.coin for h in hedged}
    assert coins_in_hedged == {"BTC"}
    result = backtest_hedged_top_k_trailing(
        hedged, top_k=2, trailing_hours=24, rebalance_hours=8,
    )
    assert result.coins_eligible == ["BTC"]
    assert "ALT" not in result.coins_eligible


# ---------------------------------------------------------------------------
# classify_regimes
# ---------------------------------------------------------------------------


def test_classify_regimes_produces_three_buckets() -> None:
    """A synthetic BTC series mixing quiet and noisy sections must produce
    classifications spanning low / med / high."""
    rng = random.Random(0)
    n = 168 * 4  # 4 windows worth
    candles: list[dict] = []
    price = 50_000.0
    for i in range(n):
        # First quarter quiet, middle noisy, last quiet.
        sigma = 0.005 if 168 <= i < 168 * 3 else 0.0002
        price *= math.exp(rng.gauss(0, sigma))
        candles.append({"t": i * HOUR_MS, "c": str(price)})
    regimes = classify_regimes(candles, vol_window_hours=168)
    names = {r.name for r in regimes.values()}
    assert names == {"low", "med", "high"}


def test_classify_regimes_low_vol_period_classified_low() -> None:
    """A flat-price hour must land in the lowest regime; a high-vol burst hour
    must land in the highest. Constructed deterministically."""
    n = 168 * 3
    candles: list[dict] = []
    price = 100.0
    for i in range(n):
        # First third: flat (zero return). Middle third: explosive. Last: flat.
        if i < 168:
            price *= 1.0
        elif i < 168 * 2:
            price *= 1.001 if i % 2 == 0 else 1 / 1.001
        else:
            price *= 1.0
        candles.append({"t": i * HOUR_MS, "c": str(price)})
    regimes = classify_regimes(candles, vol_window_hours=168)
    # The first labelled hour is at the close of the first 168-return window.
    # log_returns is computed starting from candle index 1, so the first
    # labelled timestamp is rows[168].t = hour 168.
    end_flat_window = 168 * HOUR_MS
    end_noisy_window = (168 * 2) * HOUR_MS
    # Window covering returns 1..168: all flat -> vol = 0 -> low tercile.
    assert regimes[end_flat_window].name == "low"
    # Window covering returns ~169..336: noisy oscillation -> top tercile.
    assert regimes[end_noisy_window].name == "high"


# ---------------------------------------------------------------------------
# regime_conditional_results
# ---------------------------------------------------------------------------


def test_regime_conditional_bootstrap_ci_wider_at_low_n() -> None:
    """Bootstrap CI width on Sharpe must be (weakly) larger for a smaller-N
    regime sample than a larger-N sample drawn from the same distribution.
    The small-sample CI on N=5 must be strictly wider than on N=50."""
    rng = random.Random(7)
    rebal_hours = 8

    def _make_result(n: int) -> HedgedBacktestResult:
        rebs = [
            HedgedRebalanceResult(
                t_ms_open=i * rebal_hours * HOUR_MS,
                t_ms_close=(i * rebal_hours + rebal_hours - 1) * HOUR_MS,
                coins_held=["BTC"],
                funding_received=rng.gauss(0.001, 0.0008),
                perp_pnl=0.0, spot_pnl=0.0, basis_pnl=0.0,
                entry_spread_bps=0.0, exit_spread_bps=0.0,
                net_return=rng.gauss(0.001, 0.0008),
            ) for i in range(n)
        ]
        return HedgedBacktestResult(
            n_rebalances=n, coins_eligible=["BTC"], coins_excluded_no_spot=[],
            rebalances=rebs, total_net_return=sum(r.net_return for r in rebs),
            annualized_net_return=0.0, annualized_funding_only=0.0,
            annualized_basis_pnl=0.0, annualized_spread_cost=0.0,
            sharpe=0.0, max_drawdown=0.0, hit_rate=0.0,
        )

    small = _make_result(5)
    large = _make_result(50)
    # All rebalances land in 'low' regime (single bucket fed in).
    regimes_small = {rb.t_ms_open: Regime("low", 0.001) for rb in small.rebalances}
    regimes_large = {rb.t_ms_open: Regime("low", 0.001) for rb in large.rebalances}
    rc_small = regime_conditional_results(
        small, regimes_small, rebalance_hours=rebal_hours, n_bootstrap=2000,
    )
    rc_large = regime_conditional_results(
        large, regimes_large, rebalance_hours=rebal_hours, n_bootstrap=2000,
    )
    low_small = next(r for r in rc_small if r.regime_name == "low")
    low_large = next(r for r in rc_large if r.regime_name == "low")
    width_small = low_small.sharpe_ci_high - low_small.sharpe_ci_low
    width_large = low_large.sharpe_ci_high - low_large.sharpe_ci_low
    assert width_small > width_large


# ---------------------------------------------------------------------------
# Spot listing detection (pure unit tests, no network)
# ---------------------------------------------------------------------------


def test_build_spot_index_by_base_extracts_usdc_pairs_only() -> None:
    """Only pairs quoted in USDC contribute to the {base -> @index} map."""
    spot_meta = {
        "tokens": [
            {"name": "USDC", "index": 0},
            {"name": "UBTC", "index": 197},
            {"name": "UETH", "index": 221},
            {"name": "HFUN", "index": 2},
        ],
        "universe": [
            {"name": "@142", "index": 142, "tokens": [197, 0]},  # UBTC/USDC
            {"name": "@151", "index": 151, "tokens": [221, 0]},  # UETH/USDC
            {"name": "HFUN/PURR", "index": 99, "tokens": [2, 197]},  # not USDC
        ],
    }
    by_base = _build_spot_index_by_base(spot_meta)
    assert by_base == {"UBTC": "@142", "UETH": "@151"}


def test_coin_to_spot_label_handles_u_prefix() -> None:
    """BTC (perp) -> UBTC (spot) via U-prefix fallback. AVAX -> UAVAX."""
    by_base = {"UBTC": "@142", "UETH": "@151", "PURR": "PURR/USDC"}
    assert _coin_to_spot_label("BTC", by_base) == "@142"
    assert _coin_to_spot_label("ETH", by_base) == "@151"
    assert _coin_to_spot_label("PURR", by_base) == "PURR/USDC"
    assert _coin_to_spot_label("DOGE", by_base) is None
