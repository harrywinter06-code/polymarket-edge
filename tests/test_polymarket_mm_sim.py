"""Unit tests for the market-maker simulator.

Synthetic Trade lists only — no live API. Each test pins one specific
behavior of the simulator.
"""

from __future__ import annotations

import pytest

from polymarket_edge.polymarket_mm_sim import (
    INFORMED_SCENARIO,
    MODERATE_SCENARIO,
    NAIVE_SCENARIO,
    AdverseSelectionScenario,
    Trade,
    breakeven_half_spread_fraction,
    estimate_half_spread,
    simulate_basket,
    simulate_market_maker,
)

DAY_S = 86400


def _make_trades(
    n: int,
    *,
    token: str = "tk",
    price: float = 0.5,
    size: float = 10.0,
    start_ts: int = 1_700_000_000,
    spacing_s: int = 60,
) -> list[Trade]:
    return [
        Trade(
            token_id=token,
            timestamp_s=start_ts + i * spacing_s,
            price=price,
            size_shares=size,
            taker_side="BUY",
        )
        for i in range(n)
    ]


def test_simulator_empty_trades_returns_zero_pnl() -> None:
    result = simulate_market_maker([], scenario=MODERATE_SCENARIO)
    assert result.n_trades_observed == 0
    assert result.estimated_maker_fills == 0
    assert result.gross_rebate_usd == 0.0
    assert result.adverse_selection_cost_usd == 0.0
    assert result.net_pnl_usd == 0.0
    assert result.per_day_net_usd == 0.0
    assert result.days_observed == 0.0


def test_simulator_naive_scenario_equals_pure_rebate() -> None:
    # 100 trades of $5 notional each = $500 total notional. 50% capture =
    # $250 captured. Rebate at 18.75bps = $250 * 0.001875 = $0.46875.
    trades = _make_trades(100, price=0.5, size=10.0)  # notional 5 per trade
    result = simulate_market_maker(
        trades,
        scenario=NAIVE_SCENARIO,
        sole_maker_capture_fraction=0.5,
        maker_rebate_bps_of_notional=18.75,
    )
    assert result.adverse_selection_cost_usd == 0.0
    assert result.gross_rebate_usd == pytest.approx(0.46875, rel=1e-9)
    assert result.net_pnl_usd == pytest.approx(0.46875, rel=1e-9)
    assert result.estimated_maker_fills == 50


def test_simulator_informed_scenario_lower_than_naive() -> None:
    # Use a price path with non-trivial half-spread.
    base_ts = 1_700_000_000
    trades = [
        Trade(token_id="t", timestamp_s=base_ts + i * 60, price=p, size_shares=10.0,
              taker_side="BUY")
        for i, p in enumerate(
            [0.50, 0.52, 0.50, 0.48, 0.50, 0.53, 0.49, 0.51, 0.50, 0.52] * 10
        )
    ]
    naive = simulate_market_maker(trades, scenario=NAIVE_SCENARIO)
    informed = simulate_market_maker(trades, scenario=INFORMED_SCENARIO)
    assert informed.net_pnl_usd < naive.net_pnl_usd
    assert informed.adverse_selection_cost_usd > 0.0
    assert naive.adverse_selection_cost_usd == 0.0


def test_simulator_capture_fraction_scales_linearly() -> None:
    trades = _make_trades(200, price=0.5, size=10.0)
    half = simulate_market_maker(
        trades, scenario=NAIVE_SCENARIO, sole_maker_capture_fraction=0.5
    )
    quarter = simulate_market_maker(
        trades, scenario=NAIVE_SCENARIO, sole_maker_capture_fraction=0.25
    )
    # 0.5 capture should give exactly 2x the rebate of 0.25 capture (linear in
    # captured fraction) when AS=0.
    assert half.gross_rebate_usd == pytest.approx(2 * quarter.gross_rebate_usd, rel=1e-9)
    assert half.estimated_maker_fills == 2 * quarter.estimated_maker_fills


def test_basket_aggregation_sums_per_market_results() -> None:
    trades_a = _make_trades(100, token="A", price=0.5, size=10.0)
    trades_b = _make_trades(50, token="B", price=0.5, size=10.0)
    trades_by = {"A": trades_a, "B": trades_b}
    qs = {"A": "Market A", "B": "Market B"}
    basket = simulate_basket(
        trades_by,
        qs,
        scenario=NAIVE_SCENARIO,
        event_slug="evt",
        event_title="evt",
        days_to_resolution=10.0,
    )
    indiv_a = simulate_market_maker(trades_a, scenario=NAIVE_SCENARIO, token_id="A")
    indiv_b = simulate_market_maker(trades_b, scenario=NAIVE_SCENARIO, token_id="B")
    assert basket.total_gross_rebate_usd == pytest.approx(
        indiv_a.gross_rebate_usd + indiv_b.gross_rebate_usd, rel=1e-9
    )
    assert basket.total_net_pnl_usd == pytest.approx(
        indiv_a.net_pnl_usd + indiv_b.net_pnl_usd, rel=1e-9
    )
    assert basket.n_markets_simulated == 2


def test_projection_to_resolution_handles_zero_observed_days() -> None:
    # Empty market => zero observed days => zero per-day => zero projection.
    basket = simulate_basket(
        {"A": []},
        {"A": "A"},
        scenario=MODERATE_SCENARIO,
        event_slug="evt",
        event_title="evt",
        days_to_resolution=50.0,
    )
    assert basket.days_observed == 0.0
    assert basket.per_day_net_usd == 0.0
    assert basket.projected_pnl_to_resolution_usd == 0.0
    assert basket.n_markets_simulated == 0


def test_adverse_selection_charges_match_manual_calculation() -> None:
    # Construct 21 trades with a small alternating price step so the realized
    # 5-min-ahead delta is exactly 0.02 -> half-spread = 0.01.
    base_ts = 1_700_000_000
    # spacing 60s, alternate price between 0.50 and 0.52. After 5 min (300s)
    # we look at trade i+5, so the price difference is 0.02 (since i and i+5
    # have opposite parity in this alternation). half_spread = 0.01.
    prices = []
    for i in range(40):
        prices.append(0.50 if i % 2 == 0 else 0.52)
    trades = [
        Trade(token_id="t", timestamp_s=base_ts + i * 60, price=p, size_shares=10.0,
              taker_side="BUY")
        for i, p in enumerate(prices)
    ]
    hs = estimate_half_spread(trades, window_minutes=5)
    assert hs == pytest.approx(0.01, abs=1e-9)

    custom = AdverseSelectionScenario(name="custom", realized_half_spread_fraction=1.0,
                                      description="")
    result = simulate_market_maker(
        trades,
        scenario=custom,
        sole_maker_capture_fraction=0.5,
        maker_rebate_bps_of_notional=18.75,
    )
    # Captured notional = sum(p*s) * 0.5
    captured = sum(p * 10.0 for p in prices) * 0.5
    mean_price = sum(prices) / len(prices)
    expected_as = captured * (0.01 / mean_price) * 1.0
    expected_rebate = captured * 0.001875
    assert result.adverse_selection_cost_usd == pytest.approx(expected_as, rel=1e-9)
    assert result.gross_rebate_usd == pytest.approx(expected_rebate, rel=1e-9)
    assert result.net_pnl_usd == pytest.approx(expected_rebate - expected_as, rel=1e-9)


def test_breakeven_half_spread_fraction_zeroes_net_pnl() -> None:
    # Same construction as above; the breakeven fraction applied as the
    # scenario should give net pnl ~= 0.
    base_ts = 1_700_000_000
    prices = [0.50 if i % 2 == 0 else 0.52 for i in range(40)]
    trades = [
        Trade(token_id="t", timestamp_s=base_ts + i * 60, price=p, size_shares=10.0,
              taker_side="BUY")
        for i, p in enumerate(prices)
    ]
    frac = breakeven_half_spread_fraction({"t": trades})
    assert frac > 0
    scenario = AdverseSelectionScenario(name="be", realized_half_spread_fraction=frac,
                                        description="")
    result = simulate_market_maker(trades, scenario=scenario, token_id="t")
    assert abs(result.net_pnl_usd) < 1e-9


def test_days_spanned_matches_timestamp_range() -> None:
    trades = _make_trades(2, spacing_s=DAY_S)  # 1 day apart
    result = simulate_market_maker(trades, scenario=NAIVE_SCENARIO)
    assert result.days_observed == pytest.approx(1.0, rel=1e-9)
