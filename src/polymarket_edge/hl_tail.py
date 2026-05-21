"""Tail-risk statistics for the Hyperliquid funding-capture backtest.

REDTEAM.md weakness #14 calls out that the headline reports max drawdown alone.
Senior quants ALWAYS look at the tail: Value-at-Risk at the 5% / 1% levels,
Expected Shortfall (a.k.a. CVaR) at the same levels, and the *distribution* of
drawdowns — not just the deepest one. This module computes those from a
per-period returns series produced by `hl_stats.compute_per_period_returns_trailing`
(or any other returns vector).

Conventions
-----------
Returns are signed. A return of `-0.0034` means a 0.34% loss over the period.

`var_p` is the p-th percentile of the returns series, computed via linear
interpolation on the sorted vector (the same convention used by
`hl_stats._percentile`). Interpretation:

  var_95 = -0.0034  means  "with 95% confidence the per-period return is no
                            worse than -0.34%."

i.e. only 5% of historical periods were worse than var_95. The number is the
threshold itself (negative when there's downside), NOT the absolute loss.

`expected_shortfall_p` is the mean of every return at or below `var_p` (the
canonical CVaR definition: `E[R | R <= VaR]`). With the linear-interpolation
VaR convention above, the threshold itself usually sits between two observed
returns, so `expected_shortfall_p` equals the mean of the worst ceil(n * (1-p))
observations in practice. By construction `expected_shortfall_p <= var_p` (more
negative).

Drawdowns are computed on the cumulative-sum equity curve (matching
`hl_backtest._drawdown`, which is simple-sum P&L, not geometric compounding).
`max_drawdown` is reported as a positive number (depth, like `BacktestResult`).
Durations are counted in *periods*, not hours — multiply by `hours_per_period`
if you want wall-clock time.

`max_drawdown_periods`: the longest run of consecutive periods spent strictly
below the running peak. A run begins the period AFTER a new peak is set and
ends the period that re-attains the peak. If the series ends mid-drawdown,
that run counts up to the final period.

`drawdown_recovery_periods`: periods elapsed from the trough of the *deepest*
drawdown to the next time equity meets-or-exceeds the prior peak. If the deepest
drawdown is unrecovered at series end, this is 0 (sentinel — combined with
`max_drawdown_periods > 0` it means "still underwater").
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from polymarket_edge.hl_backtest import HOURS_PER_YEAR


@dataclass(frozen=True, slots=True)
class TailStats:
    n_periods: int
    hours_per_period: int
    annualized_return: float
    var_95: float
    var_99: float
    expected_shortfall_95: float
    expected_shortfall_99: float
    max_drawdown: float
    max_drawdown_periods: int
    n_drawdown_periods: int
    drawdown_recovery_periods: int
    worst_period_return: float
    best_period_return: float


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile on a pre-sorted list. pct in [0, 100].

    Matches `hl_stats._percentile` exactly so the two modules stay consistent.
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = k - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def _annualize(per_period_return: float, hours_per_period: int) -> float:
    periods_per_year = HOURS_PER_YEAR / hours_per_period
    return per_period_return * periods_per_year


def _expected_shortfall(sorted_values: list[float], var_threshold: float) -> float:
    """Mean of returns at or below `var_threshold` (canonical CVaR).

    `sorted_values` must be ascending. Returns 0.0 for an empty input. If no
    element is at or below the threshold (impossible when threshold is the
    linear-interp percentile of the same series, but defensive), returns the
    threshold itself so ES is never less negative than VaR.
    """
    if not sorted_values:
        return 0.0
    below = [v for v in sorted_values if v <= var_threshold]
    if not below:
        return var_threshold
    return sum(below) / len(below)


def _drawdown_distribution(
    returns: Sequence[float],
) -> tuple[float, int, int, int]:
    """Walk the cumulative-sum equity curve and characterize drawdowns.

    Returns `(max_dd, max_dd_periods, n_dd_periods, recovery_periods)` where:

    - `max_dd`: deepest peak-to-trough drop (positive number).
    - `max_dd_periods`: longest run of consecutive periods spent strictly below
      the running peak. Counted in periods (return indices). A run begins the
      period after a peak is set and ends the period the equity returns to
      that peak. A run still open at end-of-series counts up to the last index.
    - `n_dd_periods`: total periods (across all runs) spent strictly below the
      running peak.
    - `recovery_periods`: periods from the trough of the deepest drawdown to the
      next period whose cumulative return >= the prior peak. 0 if unrecovered.
    """
    if not returns:
        return 0.0, 0, 0, 0

    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    # Track when/where the deepest drawdown's trough occurred.
    trough_index = -1
    trough_peak = 0.0  # peak that the deepest drawdown fell from

    current_run_len = 0
    longest_run = 0
    n_below = 0

    # equity[i] = cumulative return after period i (0-indexed)
    equity: list[float] = []
    for i, r in enumerate(returns):
        cum += r
        equity.append(cum)
        if cum >= peak:
            peak = cum
            # Run ends here (this period restored or set a new peak).
            longest_run = max(longest_run, current_run_len)
            current_run_len = 0
        else:
            current_run_len += 1
            n_below += 1
            dd = peak - cum
            if dd > max_dd:
                max_dd = dd
                trough_index = i
                trough_peak = peak

    # Series ended mid-drawdown: that open run still counts.
    longest_run = max(longest_run, current_run_len)

    # Recovery: from trough_index, find the first j > trough_index where
    # equity[j] >= trough_peak. 0 if none (still underwater at end).
    recovery_periods = 0
    if trough_index >= 0:
        for j in range(trough_index + 1, len(equity)):
            if equity[j] >= trough_peak:
                recovery_periods = j - trough_index
                break

    return max_dd, longest_run, n_below, recovery_periods


def tail_stats(
    per_period_returns: Sequence[float],
    *,
    hours_per_period: int,
) -> TailStats:
    """Compute VaR, ES, and drawdown distribution from a per-period returns series.

    See module docstring for the VaR / ES / drawdown conventions used. Returns a
    `TailStats` with all-zero fields if `per_period_returns` is empty.
    """
    n = len(per_period_returns)
    if n == 0:
        return TailStats(
            n_periods=0,
            hours_per_period=hours_per_period,
            annualized_return=0.0,
            var_95=0.0,
            var_99=0.0,
            expected_shortfall_95=0.0,
            expected_shortfall_99=0.0,
            max_drawdown=0.0,
            max_drawdown_periods=0,
            n_drawdown_periods=0,
            drawdown_recovery_periods=0,
            worst_period_return=0.0,
            best_period_return=0.0,
        )

    sorted_returns = sorted(per_period_returns)
    mean = sum(per_period_returns) / n
    ann_ret = _annualize(mean, hours_per_period)

    # VaR at 95% = 5th percentile; VaR at 99% = 1st percentile.
    var_95 = _percentile(sorted_returns, 5.0)
    var_99 = _percentile(sorted_returns, 1.0)
    es_95 = _expected_shortfall(sorted_returns, var_95)
    es_99 = _expected_shortfall(sorted_returns, var_99)

    max_dd, max_dd_periods, n_dd_periods, recovery = _drawdown_distribution(
        per_period_returns
    )

    return TailStats(
        n_periods=n,
        hours_per_period=hours_per_period,
        annualized_return=ann_ret,
        var_95=var_95,
        var_99=var_99,
        expected_shortfall_95=es_95,
        expected_shortfall_99=es_99,
        max_drawdown=max_dd,
        max_drawdown_periods=max_dd_periods,
        n_drawdown_periods=n_dd_periods,
        drawdown_recovery_periods=recovery,
        worst_period_return=sorted_returns[0],
        best_period_return=sorted_returns[-1],
    )
