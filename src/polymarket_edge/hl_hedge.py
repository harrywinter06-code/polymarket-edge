"""Spread-cost-aware variant of the Hyperliquid funding-capture backtest.

The base `backtest_top_k_trailing` measures gross funding flow only — REDTEAM.md
item 3c flags hedge-leg cost as the biggest caveat on the reported Sharpe. The
funding-capture trade is "short perp, long spot, collect funding," so every
rebalance pays round-trip slippage across four legs (enter perp, enter spot,
exit perp, exit spot).

This module nets a configurable `spread_bps_per_leg` (default 5 bps -> 20 bps
round trip) off the gross per-rebalance return. It does NOT model the spot
price leg directly — that would require a full Hyperliquid spot price feed and
the universe doesn't all have spot. The simplification is: assume the hedge is
delta-neutral by construction and only the spread cost is missing.

Helpers are duplicated from `hl_backtest` rather than re-exported so the modules
stay independent.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence

from polymarket_edge.hl_backtest import (
    HOURS_PER_YEAR,
    BacktestResult,
    FundingTick,
)


def _series_by_coin(ticks: Sequence[FundingTick]) -> dict[str, list[FundingTick]]:
    out: dict[str, list[FundingTick]] = {}
    for t in ticks:
        out.setdefault(t.coin, []).append(t)
    for k in out:
        out[k].sort(key=lambda x: x.t_ms)
    return out


def _common_grid(per_coin: dict[str, list[FundingTick]]) -> list[int]:
    if not per_coin:
        return []
    sets = [{t.t_ms for t in series} for series in per_coin.values()]
    common = set.intersection(*sets) if sets else set()
    return sorted(common)


def _maps(per_coin: dict[str, list[FundingTick]]) -> dict[str, dict[int, float]]:
    return {c: {t.t_ms: t.funding for t in series} for c, series in per_coin.items()}


def _drawdown(returns: Sequence[float]) -> float:
    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for r in returns:
        cum += r
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return abs(mdd)


def _annualize(per_period_return: float, hours_per_period: int) -> float:
    periods_per_year = HOURS_PER_YEAR / hours_per_period
    return per_period_return * periods_per_year


def _annualize_vol(per_period_std: float, hours_per_period: int) -> float:
    periods_per_year = HOURS_PER_YEAR / hours_per_period
    return per_period_std * (periods_per_year ** 0.5)


def _summary(
    *,
    strategy: str,
    top_k: int,
    rebalance_hours: int,
    trailing_hours: int,
    returns: Sequence[float],
    coins_held: set[str],
) -> BacktestResult:
    if not returns:
        return BacktestResult(strategy, 0, top_k, rebalance_hours, trailing_hours,
                              0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0)
    total = sum(returns)
    mean = statistics.fmean(returns)
    std = statistics.pstdev(returns) if len(returns) >= 2 else 0.0
    ann_ret = _annualize(mean, rebalance_hours)
    ann_vol = _annualize_vol(std, rebalance_hours)
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
    hits = sum(1 for r in returns if r > 0) / len(returns)
    return BacktestResult(
        strategy=strategy,
        n_rebalances=len(returns),
        top_k=top_k,
        rebalance_hours=rebalance_hours,
        trailing_hours=trailing_hours,
        total_return=total,
        annualized_return=ann_ret,
        annualized_vol=ann_vol,
        sharpe=sharpe,
        max_drawdown=_drawdown(returns),
        hit_rate=hits,
        n_distinct_coins_held=len(coins_held),
    )


def backtest_top_k_trailing_net_spread(
    ticks: Sequence[FundingTick],
    *,
    top_k: int = 5,
    trailing_hours: int = 24,
    rebalance_hours: int = 8,
    spread_bps_per_leg: float = 5.0,
) -> BacktestResult:
    """Same logic as `backtest_top_k_trailing` but net of round-trip spread cost.

    Each rebalance pays `4 * spread_bps_per_leg / 10_000` (enter perp, enter
    spot, exit perp, exit spot), subtracted from the realized funding sum
    before it enters the returns series. The cost is paid every rebalance
    regardless of whether the held set changed — modeling a strict re-entry
    each period is a conservative upper-bound on real cost; in practice a
    smart rebalancer would only pay for the legs that actually changed.
    """
    cost_per_rebalance = 4 * spread_bps_per_leg / 10_000
    strategy = (
        f"top{top_k}_trail{trailing_hours}h_rebal{rebalance_hours}h"
        f"_spread{spread_bps_per_leg}bp"
    )
    per_coin = _series_by_coin(ticks)
    grid = _common_grid(per_coin)
    if len(grid) < trailing_hours + rebalance_hours:
        return _summary(
            strategy=strategy,
            top_k=top_k, rebalance_hours=rebalance_hours, trailing_hours=trailing_hours,
            returns=[], coins_held=set(),
        )
    maps = _maps(per_coin)
    returns: list[float] = []
    coins_held: set[str] = set()
    i = trailing_hours
    while i + rebalance_hours <= len(grid):
        window = grid[i - trailing_hours : i]
        trail_mean: dict[str, float] = {}
        for c, m in maps.items():
            vals = [m[t] for t in window if t in m]
            if len(vals) == trailing_hours:
                trail_mean[c] = statistics.fmean(vals)
        if not trail_mean:
            i += rebalance_hours
            continue
        top = sorted(trail_mean.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        held = [c for c, _ in top]
        coins_held.update(held)
        future = grid[i : i + rebalance_hours]
        total_short_pnl = 0.0
        per_coin_count = 0
        for c in held:
            m = maps[c]
            vals = [m[t] for t in future if t in m]
            if len(vals) == len(future):
                total_short_pnl += sum(vals)
                per_coin_count += 1
        if per_coin_count > 0:
            gross = total_short_pnl / per_coin_count
            returns.append(gross - cost_per_rebalance)
        i += rebalance_hours
    return _summary(
        strategy=strategy,
        top_k=top_k, rebalance_hours=rebalance_hours, trailing_hours=trailing_hours,
        returns=returns, coins_held=coins_held,
    )


def sweep_spread_sensitivity(
    ticks: Sequence[FundingTick],
    *,
    spreads_bps: Sequence[float] = (0.0, 2.5, 5.0, 10.0, 20.0),
    top_k: int = 5,
    trailing_hours: int = 24,
    rebalance_hours: int = 8,
) -> list[BacktestResult]:
    """Run the netted backtest at each spread level. Used to answer
    "what's the Sharpe really?" — Sharpe collapses as spread rises."""
    return [
        backtest_top_k_trailing_net_spread(
            ticks,
            top_k=top_k,
            trailing_hours=trailing_hours,
            rebalance_hours=rebalance_hours,
            spread_bps_per_leg=s,
        )
        for s in spreads_bps
    ]
