"""Tests for the Hyperliquid funding-extremes directional study (Plan D)."""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path

from polymarket_edge import db
from polymarket_edge.hl_backtest import FundingTick
from polymarket_edge.hl_extremes import (
    HOUR_MS,
    TRAILING_HOURS,
    ExtremeEventResult,
    FundingPriceObservation,
    _t_stat,
    hold_to_exit,
    identify_extreme_events,
    load_candles,
    merge_funding_and_prices,
    observations_by_coin,
    persist_candles,
    run_study,
    summarize,
)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


def _funding_series(coin: str, fundings: list[float], start_ms: int = 0) -> list[FundingTick]:
    return [FundingTick(coin, start_ms + i * HOUR_MS, f) for i, f in enumerate(fundings)]


def _candles(coin: str, closes: list[float], start_ms: int = 0) -> list[tuple[int, float]]:
    return [(start_ms + i * HOUR_MS, c) for i, c in enumerate(closes)]


def _obs(
    coin: str, t_ms: int, funding: float, close: float, z: float = 0.0
) -> FundingPriceObservation:
    return FundingPriceObservation(
        coin=coin,
        t_ms=t_ms,
        funding=funding,
        perp_close=close,
        trailing_mean=0.0,
        trailing_std=1.0,
        z_score=z,
    )


# ---------------------------------------------------------------------------
# z-score behaviour
# ---------------------------------------------------------------------------


def test_z_score_on_constant_series_returns_nothing() -> None:
    """When the trailing 168h window is constant, std is 0 -> z undefined; row dropped."""
    n = TRAILING_HOURS + 5
    funding = _funding_series("BTC", [0.0001] * n)
    candles = {"BTC": _candles("BTC", [100.0] * n)}
    obs = merge_funding_and_prices(funding, candles)
    # The candidate row's funding equals the window's funding -> std=0 -> dropped.
    assert obs == []


def test_z_score_matches_manual_calc() -> None:
    """Trailing window is [1] * 168 and the candidate row is +5 stdev away.

    Setup: 168 hours of funding=1.0 then a spike of 1.0 + 5*epsilon where
    we deliberately seed a tiny non-constant baseline so std > 0. Use the
    last value of the trailing window deviating to set up a known mean/std.
    """
    base = [0.0] * (TRAILING_HOURS - 1) + [1.0]   # mean = 1/168, std > 0
    candidate = [10.0]                            # huge spike
    fundings = base + candidate
    n = len(fundings)
    funding = _funding_series("ETH", fundings)
    candles = {"ETH": _candles("ETH", [100.0] * n)}
    obs = merge_funding_and_prices(funding, candles)
    # Exactly one observation survives: the candidate (the rows before lack
    # a 168h trailing window).
    assert len(obs) == 1
    o = obs[0]
    # Manually compute expected mean/std.
    import statistics as _stat
    expected_mean = _stat.fmean(base)
    expected_std = _stat.pstdev(base)
    assert abs(o.trailing_mean - expected_mean) < 1e-12
    assert abs(o.trailing_std - expected_std) < 1e-12
    expected_z = (10.0 - expected_mean) / expected_std
    assert abs(o.z_score - expected_z) < 1e-9


def test_merge_drops_rows_without_candle() -> None:
    """A funding row whose hour has no candle close is dropped."""
    n = TRAILING_HOURS + 2
    funding = _funding_series("BTC", [float(i) for i in range(n)])
    # Drop the last candle so the last row has no close.
    closes = [100.0 + i for i in range(n - 1)]
    candles = {"BTC": _candles("BTC", closes)}
    obs = merge_funding_and_prices(funding, candles)
    # Eligible candidates are rows 168 and 169. Row 169 lacks a candle.
    assert {o.t_ms for o in obs} == {168 * HOUR_MS}


def test_merge_does_not_leak_candidate_into_its_own_window() -> None:
    """The candidate row's funding must not influence its own z-score."""
    base = [1.0] * TRAILING_HOURS
    spike = 100.0
    fundings = [*base, spike]
    funding = _funding_series("BTC", fundings)
    candles = {"BTC": _candles("BTC", [100.0] * (TRAILING_HOURS + 1))}
    obs = merge_funding_and_prices(funding, candles)
    # window is the base only -> std=0 -> row dropped, not absorbed.
    assert obs == []
    # Sanity: if we perturb base so std>0, the z is computed against base, not base+spike.
    base[0] = 0.0
    fundings = [*base, spike]
    funding = _funding_series("BTC", fundings)
    obs = merge_funding_and_prices(funding, candles)
    assert len(obs) == 1
    import statistics as _stat
    assert abs(obs[0].trailing_mean - _stat.fmean(base)) < 1e-12
    assert abs(obs[0].trailing_std - _stat.pstdev(base)) < 1e-12


# ---------------------------------------------------------------------------
# Event identification
# ---------------------------------------------------------------------------


def test_extreme_event_identification_thresholds() -> None:
    obs = [
        _obs("BTC", 0 * HOUR_MS, 0.001, 100.0, z=1.4),
        _obs("BTC", 1 * HOUR_MS, 0.002, 101.0, z=2.1),
        _obs("BTC", 2 * HOUR_MS, 0.003, 102.0, z=2.8),
        _obs("BTC", 3 * HOUR_MS, -0.002, 100.0, z=-2.5),
    ]
    pos_2 = identify_extreme_events(obs, z_threshold=2.0, direction="positive")
    assert {o.t_ms for o in pos_2} == {1 * HOUR_MS, 2 * HOUR_MS}
    pos_25 = identify_extreme_events(obs, z_threshold=2.5, direction="positive")
    assert {o.t_ms for o in pos_25} == {2 * HOUR_MS}
    neg_2 = identify_extreme_events(obs, z_threshold=2.0, direction="negative")
    assert {o.t_ms for o in neg_2} == {3 * HOUR_MS}


def test_cooldown_suppresses_consecutive_events_within_window() -> None:
    obs = [_obs("BTC", i * HOUR_MS, 0.01, 100.0, z=3.0) for i in range(10)]
    raw = identify_extreme_events(obs, z_threshold=2.0, direction="positive", cooldown_hours=0)
    assert len(raw) == 10
    cooled = identify_extreme_events(
        obs, z_threshold=2.0, direction="positive", cooldown_hours=24
    )
    # First event at t=0 fires; next eligible at t>=24h -> only one event in a 10-hour series.
    assert len(cooled) == 1
    assert cooled[0].t_ms == 0


def test_invalid_direction_raises() -> None:
    try:
        identify_extreme_events([], z_threshold=2.0, direction="sideways")
    except ValueError:
        return
    raise AssertionError("ValueError expected for invalid direction")


# ---------------------------------------------------------------------------
# Hold-to-exit
# ---------------------------------------------------------------------------


def test_hold_to_exit_drops_truncated_entries() -> None:
    series = [
        _obs("BTC", 0 * HOUR_MS, 0.001, 100.0, z=3.0),
        _obs("BTC", 1 * HOUR_MS, 0.001, 101.0),
        _obs("BTC", 2 * HOUR_MS, 0.001, 102.0),
    ]
    by_coin = {"BTC": series}
    # Hold 24h but the series only has 3 hours -> truncated, drop.
    events = hold_to_exit([series[0]], by_coin, hold_hours=24)
    assert events == []
    # Hold 2h -> exit at t=2, full window available, should produce one event.
    events = hold_to_exit([series[0]], by_coin, hold_hours=2)
    assert len(events) == 1
    e = events[0]
    assert e.exit_t_ms == 2 * HOUR_MS
    assert math.isclose(e.price_return, 102.0 / 100.0 - 1.0)


def test_long_net_return_subtracts_funding_paid() -> None:
    series = [
        _obs("BTC", 0 * HOUR_MS, 0.01, 100.0, z=3.0),
        _obs("BTC", 1 * HOUR_MS, 0.02, 105.0),
        _obs("BTC", 2 * HOUR_MS, 0.03, 110.0),
    ]
    by_coin = {"BTC": series}
    events = hold_to_exit([series[0]], by_coin, hold_hours=2)
    assert len(events) == 1
    e = events[0]
    # Funding paid is hours 1 and 2 (strictly after entry, up to exit).
    assert math.isclose(e.funding_paid_long, 0.02 + 0.03)
    assert math.isclose(e.price_return, 110.0 / 100.0 - 1.0)
    assert math.isclose(e.long_net_return, (110.0 / 100.0 - 1.0) - (0.02 + 0.03))


def test_short_net_return_signs_inverse_of_long() -> None:
    series = [
        _obs("BTC", 0 * HOUR_MS, 0.01, 100.0, z=3.0),
        _obs("BTC", 1 * HOUR_MS, 0.02, 110.0),
    ]
    by_coin = {"BTC": series}
    events = hold_to_exit([series[0]], by_coin, hold_hours=1)
    assert len(events) == 1
    e = events[0]
    assert math.isclose(e.long_net_return, -e.short_net_return)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def test_summarize_handles_zero_events() -> None:
    s = summarize([], z_threshold=2.0, direction="positive", hold_hours=24)
    assert s.n_events == 0
    assert s.n_coins == 0
    assert s.long_sharpe == 0.0
    assert s.short_sharpe == 0.0
    assert s.long_t_stat == 0.0
    assert s.short_t_stat == 0.0


def test_summarize_n_coins_distinct() -> None:
    events = [
        ExtremeEventResult(
            coin=c, entry_t_ms=0, entry_z=3.0, entry_funding=0.01,
            entry_price=100.0, exit_t_ms=HOUR_MS, exit_price=101.0,
            price_return=0.01, funding_paid_long=0.005,
            long_net_return=0.005, short_net_return=-0.005, hold_hours=1,
        )
        for c in ("BTC", "ETH", "BTC", "SOL")
    ]
    s = summarize(events, z_threshold=2.0, direction="positive", hold_hours=1)
    assert s.n_events == 4
    assert s.n_coins == 3


def test_t_stat_inline_formula() -> None:
    """t-stat = mean / (sample_std / sqrt(N)); sanity-check against a known case."""
    # values = [1, 2, 3, 4, 5] -> mean=3, sample_std=sqrt(2.5)=1.5811, N=5
    # t = 3 / (1.5811 / sqrt(5)) = 3 / 0.7071 = 4.2426
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert math.isclose(_t_stat(values), 4.242640687119286, rel_tol=1e-9)
    assert _t_stat([]) == 0.0
    assert _t_stat([5.0]) == 0.0
    assert _t_stat([1.0, 1.0, 1.0]) == 0.0  # zero variance


def test_sharpe_annualization_convention_documented() -> None:
    """Sharpe at 24h hold uses periods_per_year = 365 -> sqrt(365) multiplier.

    Per-event mean=0.01, std=0.02 -> Sharpe-per-period = 0.5.
    Annualised at 24h: 0.5 * sqrt(365) ~= 9.5526.
    """
    events = [
        ExtremeEventResult(
            coin="BTC", entry_t_ms=i * HOUR_MS, entry_z=3.0, entry_funding=0.01,
            entry_price=100.0, exit_t_ms=(i + 1) * HOUR_MS, exit_price=100.0,
            price_return=0.0, funding_paid_long=0.0,
            long_net_return=v, short_net_return=-v, hold_hours=24,
        )
        for i, v in enumerate([-0.01, 0.01, -0.01, 0.03, 0.03])  # mean=0.01 std=0.02
    ]
    s = summarize(events, z_threshold=2.0, direction="positive", hold_hours=24)
    # mean=0.01, sample_std=0.02 -> per-period Sharpe 0.5, x sqrt(365) ~= 9.55
    assert math.isclose(s.long_sharpe, 0.5 * math.sqrt(365.0), rel_tol=1e-9)


def test_run_study_end_to_end_synthetic() -> None:
    """One coin, one synthetic spike, verify a full study run."""
    base = [0.0] * (TRAILING_HOURS - 1) + [1.0]  # std>0
    fundings = [*base, 10.0, 0.0, 0.0]           # candidate is hour 168, exits at 170
    funding = _funding_series("BTC", fundings)
    closes = [100.0] * TRAILING_HOURS + [100.0, 105.0, 110.0]
    candles = {"BTC": _candles("BTC", closes)}
    obs = merge_funding_and_prices(funding, candles)
    s = run_study(
        obs, z_threshold=2.0, direction="positive", hold_hours=2, cooldown_hours=0
    )
    assert s.n_events == 1
    e = s.events[0]
    assert math.isclose(e.entry_price, 100.0)
    assert math.isclose(e.exit_price, 110.0)
    assert math.isclose(e.price_return, 0.10)
    assert math.isclose(e.funding_paid_long, 0.0)


# ---------------------------------------------------------------------------
# DB persistence round-trip
# ---------------------------------------------------------------------------


def test_persist_and_load_candles_round_trip(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "test.db")
    db.init_schema(conn, schema_path=SCHEMA_PATH)
    rows = {
        "BTC": [(1_700_000_000_000, 50_000.0), (1_700_003_600_000, 50_100.0)],
        "ETH": [(1_700_000_000_000, 2_500.0)],
    }
    n = persist_candles(conn, rows, fetched_at="2026-05-21T00:00:00+00:00")
    conn.commit()
    assert n == 3
    loaded = load_candles(conn)
    assert loaded == rows


def test_observations_by_coin_sorted() -> None:
    obs = [
        _obs("ETH", 2 * HOUR_MS, 0.0, 100.0),
        _obs("BTC", 1 * HOUR_MS, 0.0, 100.0),
        _obs("BTC", 0 * HOUR_MS, 0.0, 100.0),
    ]
    by_coin = observations_by_coin(obs)
    assert list(by_coin.keys()) == ["ETH", "BTC"] or list(by_coin.keys()) == ["BTC", "ETH"]
    assert [o.t_ms for o in by_coin["BTC"]] == [0, HOUR_MS]
