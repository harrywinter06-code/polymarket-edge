"""Bootstrap confidence intervals for the Hyperliquid funding-capture backtest.

The base `backtest_top_k_trailing` reports a point estimate over N=56 rebalances
(30-day sample). A Sharpe of 37 on N=56 has wide error bars, and the founder-
facing claim needs them stated. This module implements a standard nonparametric
percentile bootstrap over the per-period returns series:

  - Resample the per-period return vector with replacement, same length.
  - Compute annualized return and Sharpe on each resample using the same
    formulas as `hl_backtest._annualize` / `_annualize_vol` (mean * periods/yr;
    pstdev * sqrt(periods/yr); Sharpe = ann_ret / ann_vol).
  - Return the 2.5th / 97.5th percentile of each statistic.

The existing `backtest_top_k_trailing` returns a `BacktestResult` aggregate and
does not expose the per-period returns. Rather than break that signature, this
module ships `compute_per_period_returns_trailing` — a duplicate of the trailing
loop that returns the raw per-period list. Same helpers are duplicated from
`hl_backtest` for module independence (matches the `hl_hedge.py` pattern).
"""

from __future__ import annotations

import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from polymarket_edge.hl_backtest import HOURS_PER_YEAR, FundingTick


@dataclass(frozen=True, slots=True)
class StatWithCI:
    point: float
    ci_low: float    # 2.5th percentile
    ci_high: float   # 97.5th percentile


@dataclass(frozen=True, slots=True)
class BacktestStats:
    annualized_return: StatWithCI
    sharpe: StatWithCI
    n_resamples: int


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


def _annualize(per_period_return: float, hours_per_period: int) -> float:
    periods_per_year = HOURS_PER_YEAR / hours_per_period
    return per_period_return * periods_per_year


def _annualize_vol(per_period_std: float, hours_per_period: int) -> float:
    periods_per_year = HOURS_PER_YEAR / hours_per_period
    return per_period_std * (periods_per_year ** 0.5)


def _ann_ret_and_sharpe(returns: Sequence[float], hours_per_period: int) -> tuple[float, float]:
    if not returns:
        return 0.0, 0.0
    mean = statistics.fmean(returns)
    std = statistics.pstdev(returns) if len(returns) >= 2 else 0.0
    ann_ret = _annualize(mean, hours_per_period)
    ann_vol = _annualize_vol(std, hours_per_period)
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
    return ann_ret, sharpe


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile on a pre-sorted list. pct in [0, 100]."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = k - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def compute_per_period_returns_trailing(
    ticks: Sequence[FundingTick],
    *,
    top_k: int,
    trailing_hours: int,
    rebalance_hours: int,
) -> list[float]:
    """Re-run the `backtest_top_k_trailing` loop and return the per-period
    return list (the input to bootstrap resampling). Duplicates the existing
    loop verbatim so a refactor of `hl_backtest.backtest_top_k_trailing` is
    not required."""
    per_coin = _series_by_coin(ticks)
    grid = _common_grid(per_coin)
    if len(grid) < trailing_hours + rebalance_hours:
        return []
    maps = _maps(per_coin)
    returns: list[float] = []
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
    return returns


def bootstrap_backtest_stats(
    per_period_returns: list[float],
    *,
    hours_per_period: int,
    n_resamples: int = 5000,
    seed: int = 42,
) -> BacktestStats:
    """Standard nonparametric percentile bootstrap.

    Resamples the per-period return vector with replacement (same length) and
    recomputes annualized return + Sharpe on each draw. Returns the 2.5th /
    97.5th percentiles as the 95% CI bounds. The point estimate is computed
    on the original (non-resampled) series.

    Sharpe uses `pstdev` (population stdev) to match `hl_backtest._summary`.
    A constant-return resample yields std=0 and Sharpe=0 by the same fallback
    convention as the base backtest.
    """
    point_ann, point_sharpe = _ann_ret_and_sharpe(per_period_returns, hours_per_period)

    if not per_period_returns or n_resamples <= 0:
        zero = StatWithCI(point=point_ann, ci_low=point_ann, ci_high=point_ann)
        zero_sharpe = StatWithCI(point=point_sharpe, ci_low=point_sharpe, ci_high=point_sharpe)
        return BacktestStats(
            annualized_return=zero,
            sharpe=zero_sharpe,
            n_resamples=n_resamples,
        )

    rng = random.Random(seed)
    n = len(per_period_returns)
    ann_returns: list[float] = []
    sharpes: list[float] = []
    for _ in range(n_resamples):
        sample = [per_period_returns[rng.randrange(n)] for _ in range(n)]
        ann_ret, sharpe = _ann_ret_and_sharpe(sample, hours_per_period)
        ann_returns.append(ann_ret)
        sharpes.append(sharpe)

    ann_returns.sort()
    sharpes.sort()
    return BacktestStats(
        annualized_return=StatWithCI(
            point=point_ann,
            ci_low=_percentile(ann_returns, 2.5),
            ci_high=_percentile(ann_returns, 97.5),
        ),
        sharpe=StatWithCI(
            point=point_sharpe,
            ci_low=_percentile(sharpes, 2.5),
            ci_high=_percentile(sharpes, 97.5),
        ),
        n_resamples=n_resamples,
    )
