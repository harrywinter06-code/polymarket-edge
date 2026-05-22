"""Live deliverable: market-maker P&L simulation on the 2026 FIFA World Cup
negRisk basket.

Pulls the World Cup event from gamma, fetches trade history for each
constituent market via the public data-api /trades endpoint, then runs the
simulator under naive / moderate / informed adverse-selection scenarios. Also
sweeps maker-capture fractions and reports the breakeven half-spread fraction
plus per-market top/bottom contributors.

Result JSON is saved to ``results/world_cup_mm_<timestamp>.json`` (gitignored).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from polymarket_edge.polymarket_mm_sim import (
    MODERATE_SCENARIO,
    SCENARIOS,
    EventMMResult,
    Trade,
    breakeven_half_spread_fraction,
    fetch_trades_for_event,
    simulate_basket,
)

EVENT_SLUG = "2026-fifa-world-cup-winner-595"
EVENT_TITLE = "2026 FIFA World Cup Winner"
DAYS_TO_RESOLUTION = 50.0
LOOKBACK_DAYS = 30
GAMMA_BASE = "https://gamma-api.polymarket.com"


async def _fetch_event(slug: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{GAMMA_BASE}/events", params={"slug": slug})
        r.raise_for_status()
        events = r.json()
        if not events:
            raise RuntimeError(f"event slug {slug!r} not found on gamma")
        return events[0] if isinstance(events, list) else events


def _market_question_map(markets: list[dict[str, Any]]) -> dict[str, str]:
    """Map yes_token -> market question."""
    out: dict[str, str] = {}
    for m in markets:
        raw = m.get("clobTokenIds")
        if not raw:
            continue
        try:
            tokens = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        if not tokens:
            continue
        out[str(tokens[0])] = str(m.get("question") or "")
    return out


def _format_money(x: float) -> str:
    if abs(x) >= 1_000:
        return f"${x:,.0f}"
    if abs(x) >= 1:
        return f"${x:,.2f}"
    return f"${x:.4f}"


def _print_event_header(event: dict[str, Any], markets: list[dict[str, Any]]) -> None:
    print(f"event: {event.get('title') or EVENT_TITLE}")
    print(f"  active markets: {len(markets)}")
    print(f"  observation window: up to {LOOKBACK_DAYS} days (capped by 3000-trade offset)")
    print("  maker rebate: 18.75 bps of notional (Sports, 25% of 0.75% taker fee)")
    print()


def _print_scenario(result: EventMMResult) -> None:
    print(
        f"  {result.scenario.name:<11}: "
        f"gross rebate {_format_money(result.total_gross_rebate_usd)}  "
        f"AS {_format_money(result.total_adverse_selection_usd)}  "
        f"net {_format_money(result.total_net_pnl_usd)}  "
        f"per-day {_format_money(result.per_day_net_usd)}  "
        f"projected to {DAYS_TO_RESOLUTION:.0f}d "
        f"{_format_money(result.projected_pnl_to_resolution_usd)}"
    )


def _print_capture_sensitivity(
    trades_by_token: dict[str, list[Trade]],
    market_questions: dict[str, str],
) -> None:
    print("\nSensitivity to maker capture fraction (moderate AS):")
    for frac in [0.25, 0.50, 0.75]:
        r = simulate_basket(
            trades_by_token,
            market_questions,
            scenario=MODERATE_SCENARIO,
            event_slug=EVENT_SLUG,
            event_title=EVENT_TITLE,
            days_to_resolution=DAYS_TO_RESOLUTION,
            sole_maker_capture_fraction=frac,
        )
        print(
            f"  {frac:.2f}: per-day {_format_money(r.per_day_net_usd)}  "
            f"projected {_format_money(r.projected_pnl_to_resolution_usd)}"
        )


def _print_per_market(result: EventMMResult, *, top: int = 5) -> None:
    contributors = sorted(
        result.per_market, key=lambda r: r.net_pnl_usd, reverse=True
    )
    nonzero = [r for r in contributors if r.n_trades_observed > 0]
    print(f"\nTop {top} markets by net P&L contribution ({result.scenario.name} scenario):")
    for r in nonzero[:top]:
        q = (r.market_question or r.token_id)[:55]
        print(
            f"  net {_format_money(r.net_pnl_usd):>12}  per-day "
            f"{_format_money(r.per_day_net_usd):>10}  "
            f"trades {r.n_trades_observed:>5}  {q}"
        )
    print(f"\nBottom {top}:")
    for r in nonzero[-top:]:
        q = (r.market_question or r.token_id)[:55]
        print(
            f"  net {_format_money(r.net_pnl_usd):>12}  per-day "
            f"{_format_money(r.per_day_net_usd):>10}  "
            f"trades {r.n_trades_observed:>5}  {q}"
        )


def _to_json_safe(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_json_safe(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _to_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_json_safe(x) for x in obj]
    if isinstance(obj, tuple):
        return [_to_json_safe(x) for x in obj]
    return obj


async def main() -> None:
    started = time.time()
    print(f"fetching event {EVENT_SLUG!r}...", file=sys.stderr)
    event = await _fetch_event(EVENT_SLUG)
    markets = [m for m in event.get("markets", []) if m.get("active") and not m.get("closed")]
    market_questions = _market_question_map(markets)

    _print_event_header(event, markets)
    print(
        f"fetching {LOOKBACK_DAYS}d trade history for {len(markets)} markets...",
        file=sys.stderr,
    )
    trades_by_token = await fetch_trades_for_event(markets, lookback_days=LOOKBACK_DAYS)
    n_trades_total = sum(len(v) for v in trades_by_token.values())
    n_markets_with_trades = sum(1 for v in trades_by_token.values() if v)
    elapsed = time.time() - started
    print(
        f"  pulled {n_trades_total:,} trades across {n_markets_with_trades}/"
        f"{len(markets)} markets in {elapsed:.1f}s",
        file=sys.stderr,
    )

    # Per-scenario at capture=0.5
    print("\nPer-scenario results (sole_maker_capture_fraction=0.5):")
    results: dict[str, EventMMResult] = {}
    for scenario in SCENARIOS:
        r = simulate_basket(
            trades_by_token,
            market_questions,
            scenario=scenario,
            event_slug=EVENT_SLUG,
            event_title=EVENT_TITLE,
            days_to_resolution=DAYS_TO_RESOLUTION,
            sole_maker_capture_fraction=0.5,
        )
        results[scenario.name] = r
        _print_scenario(r)

    _print_capture_sensitivity(trades_by_token, market_questions)

    be = breakeven_half_spread_fraction(trades_by_token)
    print(f"\nBreakeven half-spread fraction: {be:.3f}")
    print(f"  -> net P&L is zero when AS = {be:.3f} of spread; positive below that")

    moderate = results["moderate"]
    _print_per_market(moderate, top=5)

    # Save
    results_dir = Path(__file__).resolve().parent.parent / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    out_path = results_dir / f"world_cup_mm_{timestamp}.json"

    payload = {
        "event_slug": EVENT_SLUG,
        "event_title": EVENT_TITLE,
        "ran_at": datetime.now(UTC).isoformat(),
        "lookback_days": LOOKBACK_DAYS,
        "days_to_resolution": DAYS_TO_RESOLUTION,
        "n_markets_in_event": len(markets),
        "n_trades_total": n_trades_total,
        "n_markets_with_trades": n_markets_with_trades,
        "breakeven_half_spread_fraction": be,
        "scenarios": {name: _to_json_safe(r) for name, r in results.items()},
        "capture_sensitivity_moderate": {
            f"{f:.2f}": _to_json_safe(
                simulate_basket(
                    trades_by_token,
                    market_questions,
                    scenario=MODERATE_SCENARIO,
                    event_slug=EVENT_SLUG,
                    event_title=EVENT_TITLE,
                    days_to_resolution=DAYS_TO_RESOLUTION,
                    sole_maker_capture_fraction=f,
                )
            )
            for f in (0.25, 0.50, 0.75)
        },
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nSaved to {out_path.relative_to(Path.cwd())}")


if __name__ == "__main__":
    asyncio.run(main())
