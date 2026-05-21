"""Alternative funding-capture strategies for Hyperliquid perpetuals.

The base `backtest_top_k_trailing` ranks coins by the *level* of trailing-mean
funding. The obvious follow-on question is whether the *change* in funding
predicts better — i.e., does "funding is rising relative to a longer baseline"
beat "funding is high in absolute terms"?

This module implements `backtest_funding_momentum`, which ranks coins by a
z-score of (short-window mean - long-window mean) / pstdev(long_window). The
short window is the recent signal, the long window is the baseline; coins
whose recent funding is unusually high relative to their own history rank
highest.

Mechanics match `backtest_top_k_trailing` exactly: at each rebalance tick,
both windows end strictly before the rebalance point (no look-ahead), the
funding flow over the next `rebalance_hours` is realized as the short-side
P&L, and a full future window per coin is required to avoid partial-data
bias.

Helpers are duplicated from `hl_backtest` so the modules stay independent —
same pattern as `hl_hedge.py`.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence

from polymarket_edge.hl_backtest import HOURS_PER_YEAR, BacktestResult, FundingTick


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


def backtest_funding_momentum(
    ticks: Sequence[FundingTick],
    *,
    top_k: int = 5,
    short_window_hours: int = 24,
    long_window_hours: int = 168,
    rebalance_hours: int = 8,
) -> BacktestResult:
    """Rank coins by z-score: (mean(short_window) - mean(long_window)) / pstdev(long_window).

    Tests whether 'rising funding relative to recent baseline' beats 'high
    funding'. Same realize-the-funding-over-next-window mechanic as
    `backtest_top_k_trailing`; same look-ahead protection (both windows end
    strictly before the rebalance point).

    `trailing_hours` in the returned `BacktestResult` carries the long window
    size (the dominant history requirement). Coins whose long-window pstdev is
    zero are skipped for that rebalance (degenerate z-score).
    """
    strategy = (
        f"momentum_top{top_k}_short{short_window_hours}h_long{long_window_hours}h"
        f"_rebal{rebalance_hours}h"
    )
    if short_window_hours <= 0 or long_window_hours <= 0:
        raise ValueError("window sizes must be positive")
    if short_window_hours > long_window_hours:
        raise ValueError("short_window_hours must be <= long_window_hours")

    per_coin = _series_by_coin(ticks)
    grid = _common_grid(per_coin)
    if len(grid) < long_window_hours + rebalance_hours:
        return _summary(
            strategy=strategy,
            top_k=top_k, rebalance_hours=rebalance_hours, trailing_hours=long_window_hours,
            returns=[], coins_held=set(),
        )
    maps = _maps(per_coin)
    returns: list[float] = []
    coins_held: set[str] = set()
    i = long_window_hours
    while i + rebalance_hours <= len(grid):
        long_window = grid[i - long_window_hours : i]
        short_window = grid[i - short_window_hours : i]
        scores: dict[str, float] = {}
        for c, m in maps.items():
            long_vals = [m[t] for t in long_window if t in m]
            short_vals = [m[t] for t in short_window if t in m]
            if len(long_vals) != long_window_hours or len(short_vals) != short_window_hours:
                continue
            long_std = statistics.pstdev(long_vals) if len(long_vals) >= 2 else 0.0
            if long_std == 0.0:
                continue
            z = (statistics.fmean(short_vals) - statistics.fmean(long_vals)) / long_std
            scores[c] = z
        if not scores:
            i += rebalance_hours
            continue
        top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
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
            returns.append(total_short_pnl / per_coin_count)
        i += rebalance_hours
    return _summary(
        strategy=strategy,
        top_k=top_k, rebalance_hours=rebalance_hours, trailing_hours=long_window_hours,
        returns=returns, coins_held=coins_held,
    )
