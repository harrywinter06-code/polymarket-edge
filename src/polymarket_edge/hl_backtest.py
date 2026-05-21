"""Funding-capture backtest for Hyperliquid perpetuals.

We test a top-K trailing-window funding-capture strategy:

  - Pull N days of hourly funding history per coin from `hl_funding_history`.
  - At each rebalance tick (every `rebalance_hours`):
      1. For each coin, compute the trailing-window mean funding rate (the predictor).
      2. Rank coins by trailing mean.
      3. "Short" the top K coins (equal-weight) for the next `rebalance_hours` interval.
      4. Realize the actual funding paid over that interval (this is the P&L).

Hedge assumption: a costless delta hedge holds. We do NOT simulate basis risk,
spot funding, or liquidation. Real net P&L will be lower; the result is an
upper bound on the carry available.

Two reference benchmarks:
  - PERFECT_HINDSIGHT: each interval, short the realized-highest-funding coin
    (look-ahead). This is the carry ceiling for K=1.
  - PASSIVE_BTC: always short BTC. The naive baseline.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

HOURS_PER_YEAR = 24 * 365


@dataclass(frozen=True, slots=True)
class FundingTick:
    coin: str
    t_ms: int
    funding: float


@dataclass(frozen=True, slots=True)
class BacktestResult:
    strategy: str
    n_rebalances: int
    top_k: int
    rebalance_hours: int
    trailing_hours: int
    total_return: float          # sum of per-interval returns
    annualized_return: float
    annualized_vol: float
    sharpe: float                # mean / std * sqrt(n_periods_per_year), risk-free = 0
    max_drawdown: float
    hit_rate: float              # % of intervals with positive return
    n_distinct_coins_held: int


def load_funding(
    conn: sqlite3.Connection,
    *,
    coin: str | None = None,
    min_t: int | None = None,
) -> list[FundingTick]:
    sql = "SELECT coin, t, funding FROM hl_funding_history"
    where: list[str] = []
    params: list[object] = []
    if coin is not None:
        where.append("coin = ?")
        params.append(coin)
    if min_t is not None:
        where.append("t >= ?")
        params.append(min_t)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY t ASC"
    return [
        FundingTick(coin=r[0], t_ms=int(r[1]), funding=float(r[2]))
        for r in conn.execute(sql, params).fetchall()
    ]


def _series_by_coin(ticks: Sequence[FundingTick]) -> dict[str, list[FundingTick]]:
    out: dict[str, list[FundingTick]] = {}
    for t in ticks:
        out.setdefault(t.coin, []).append(t)
    for k in out:
        out[k].sort(key=lambda x: x.t_ms)
    return out


def _common_grid(per_coin: dict[str, list[FundingTick]]) -> list[int]:
    """Build the union of timestamps that appear in every coin's series."""
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


def backtest_top_k_trailing(
    ticks: Sequence[FundingTick],
    *,
    top_k: int = 5,
    trailing_hours: int = 24,
    rebalance_hours: int = 8,
) -> BacktestResult:
    """Trailing-mean predictor: rank by trailing N-hour mean, short the top K
    for the next rebalance_hours interval, realize the actual funding sum.
    Returns are SHORT-side, so positive funding (paid by longs) is profit.
    """
    per_coin = _series_by_coin(ticks)
    grid = _common_grid(per_coin)
    if len(grid) < trailing_hours + rebalance_hours:
        return _summary(
            strategy=f"top{top_k}_trail{trailing_hours}h_rebal{rebalance_hours}h",
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
        # Realize funding over the next rebalance_hours window. Require a
        # complete future window per coin to avoid the partial-data bias.
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
        strategy=f"top{top_k}_trail{trailing_hours}h_rebal{rebalance_hours}h",
        top_k=top_k, rebalance_hours=rebalance_hours, trailing_hours=trailing_hours,
        returns=returns, coins_held=coins_held,
    )


def backtest_perfect_hindsight(
    ticks: Sequence[FundingTick],
    *,
    top_k: int = 1,
    rebalance_hours: int = 8,
) -> BacktestResult:
    """Cheating baseline — at each interval, short the coin that actually had
    the highest funding over that interval. Upper bound for K=1."""
    per_coin = _series_by_coin(ticks)
    grid = _common_grid(per_coin)
    maps = _maps(per_coin)
    returns: list[float] = []
    coins_held: set[str] = set()
    i = 0
    while i + rebalance_hours <= len(grid):
        future = grid[i : i + rebalance_hours]
        realized: dict[str, float] = {}
        for c, m in maps.items():
            vals = [m[t] for t in future if t in m]
            if len(vals) == rebalance_hours:
                realized[c] = sum(vals)
        if not realized:
            i += rebalance_hours
            continue
        top = sorted(realized.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        per_period = sum(v for _, v in top) / len(top)
        coins_held.update(c for c, _ in top)
        returns.append(per_period)
        i += rebalance_hours
    return _summary(
        strategy=f"perfect_top{top_k}_rebal{rebalance_hours}h",
        top_k=top_k, rebalance_hours=rebalance_hours, trailing_hours=0,
        returns=returns, coins_held=coins_held,
    )


def backtest_passive(
    ticks: Sequence[FundingTick],
    *,
    coin: str,
    rebalance_hours: int = 8,
) -> BacktestResult:
    """Always short one coin. Naive baseline."""
    series = [t for t in ticks if t.coin == coin]
    series.sort(key=lambda x: x.t_ms)
    returns: list[float] = []
    for i in range(0, len(series) - rebalance_hours + 1, rebalance_hours):
        chunk = series[i : i + rebalance_hours]
        if len(chunk) < rebalance_hours:
            break
        returns.append(sum(t.funding for t in chunk))
    return _summary(
        strategy=f"passive_short_{coin}_rebal{rebalance_hours}h",
        top_k=1, rebalance_hours=rebalance_hours, trailing_hours=0,
        returns=returns, coins_held={coin},
    )
