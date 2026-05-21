"""Block-bootstrap confidence intervals for the Hyperliquid funding-capture backtest.

The IID percentile bootstrap in `hl_stats.bootstrap_backtest_stats` resamples the
per-period return vector with replacement, treating consecutive draws as
independent. Funding-rate returns are *not* independent — a coin paying
+3 bps/hr today is more likely to pay +3 bps/hr tomorrow (regime persistence).
IID resampling on autocorrelated returns destroys the serial structure and
understates the variance of long-run statistics like the annualized Sharpe.

This module implements two block-bootstrap variants that preserve local
autocorrelation:

  - Künsch (1989) **moving-block bootstrap**: sample fixed-length blocks of
    consecutive observations with replacement, concatenate to the original
    length. Block starts can be any position in [0, n - block_length].
  - Politis & Romano (1994) **stationary bootstrap**: blocks have geometrically
    distributed random lengths with mean `block_length`, producing a strictly
    stationary resampled series. Avoids the edge artefacts of fixed-block
    methods.

For block-length selection we ship a stdlib Politis-White-style heuristic:
find the first lag where the sample ACF drops below the white-noise bound
`2/sqrt(n)`, fall back to `floor(n^(1/3))` if no such lag exists in the search
range. Lower-bounded at 1.

The module is a *pure addition* — it does not edit `hl_stats.py` or any other
existing file. It re-uses `StatWithCI` from `hl_stats` for return-type
consistency, and reuses the same annualization conventions
(`HOURS_PER_YEAR / hours_per_period`; mean * periods/yr; pstdev * sqrt(...)).
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from polymarket_edge.hl_backtest import HOURS_PER_YEAR
from polymarket_edge.hl_stats import StatWithCI


@dataclass(frozen=True, slots=True)
class BlockBootstrapStats:
    """Result of a block-bootstrap run.

    Same shape as `hl_stats.BacktestStats` but adds `block_length` and
    `method` metadata so a caller comparing variants can tell them apart.
    """

    annualized_return: StatWithCI
    sharpe: StatWithCI
    n_resamples: int
    block_length: int
    method: str  # 'moving-block' or 'stationary'


# ---------------------------------------------------------------------------
# Internal helpers (kept module-private; duplicated from hl_stats to keep
# this file independent — same pattern hl_stats uses against hl_backtest).
# ---------------------------------------------------------------------------


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


def _autocorrelation(returns: Sequence[float], lag: int) -> float:
    """Biased sample ACF at integer lag >= 1.

    Uses the standard biased estimator (divides by n, not n - lag) so the
    sequence of ACFs is positive semi-definite and the white-noise bound
    `2 / sqrt(n)` applies cleanly. Constant or near-constant series return 0.
    """
    n = len(returns)
    if lag <= 0 or lag >= n:
        return 0.0
    mean = statistics.fmean(returns)
    var = sum((x - mean) ** 2 for x in returns) / n
    if var <= 0.0:
        return 0.0
    cov = sum((returns[i] - mean) * (returns[i + lag] - mean) for i in range(n - lag)) / n
    return cov / var


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def estimate_optimal_block_length(
    returns: Sequence[float],
    *,
    max_lag: int | None = None,
) -> int:
    """Politis-White-style automatic block-length selector.

    Heuristic suitable for small N: walk lags 1..max_lag and return the first
    lag k at which `|acf(k)| < 2 / sqrt(n)` (the standard white-noise bound).
    The chosen block length is then `k` — long enough to span the
    significant autocorrelation, no longer than necessary.

    If every lag in the search range is significant (strong long-memory or
    very small n), fall back to `floor(n ** (1/3))`, which is the
    Politis-White optimal-rate cap.

    Always returns an int >= 1.
    """
    n = len(returns)
    if n < 2:
        return 1
    if max_lag is None:
        max_lag = min(n - 1, max(1, round(10 * math.log10(n))))
    max_lag = max(1, min(max_lag, n - 1))

    bound = 2.0 / math.sqrt(n)
    for lag in range(1, max_lag + 1):
        if abs(_autocorrelation(returns, lag)) < bound:
            return max(1, lag)
    fallback = int(n ** (1.0 / 3.0))
    return max(1, fallback)


def _bootstrap_ci_from_samples(
    point_ann: float,
    point_sharpe: float,
    ann_returns: list[float],
    sharpes: list[float],
    *,
    n_resamples: int,
    block_length: int,
    method: str,
) -> BlockBootstrapStats:
    if not ann_returns:
        flat_ann = StatWithCI(point=point_ann, ci_low=point_ann, ci_high=point_ann)
        flat_sharpe = StatWithCI(point=point_sharpe, ci_low=point_sharpe, ci_high=point_sharpe)
        return BlockBootstrapStats(
            annualized_return=flat_ann,
            sharpe=flat_sharpe,
            n_resamples=n_resamples,
            block_length=block_length,
            method=method,
        )
    ann_returns.sort()
    sharpes.sort()
    return BlockBootstrapStats(
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
        block_length=block_length,
        method=method,
    )


def moving_block_bootstrap(
    returns: Sequence[float],
    *,
    hours_per_period: int,
    block_length: int,
    n_resamples: int = 5000,
    seed: int = 42,
) -> BlockBootstrapStats:
    """Künsch moving-block bootstrap.

    For each resample, draw `ceil(n / block_length)` block-start indices
    uniformly from `[0, n - block_length]`, take `block_length` consecutive
    observations from each, concatenate, and truncate to length `n`. Compute
    annualized return + Sharpe on each resample; return 2.5 / 97.5 percentiles.

    Wraparound is avoided by restricting block starts to the in-bounds range
    `[0, n - block_length]` (Künsch's original construction). For very small
    series where `block_length >= n`, the entire series is taken as a single
    block — the only well-defined behaviour.
    """
    point_ann, point_sharpe = _ann_ret_and_sharpe(returns, hours_per_period)
    n = len(returns)
    if not returns or n_resamples <= 0:
        return _bootstrap_ci_from_samples(
            point_ann, point_sharpe, [], [],
            n_resamples=n_resamples, block_length=max(1, block_length),
            method="moving-block",
        )

    bl = max(1, min(int(block_length), n))
    rng = random.Random(seed)
    n_blocks = math.ceil(n / bl)
    max_start = n - bl  # inclusive upper bound; 0 if bl == n

    ann_returns: list[float] = []
    sharpes: list[float] = []
    for _ in range(n_resamples):
        sample: list[float] = []
        for _b in range(n_blocks):
            start = 0 if max_start <= 0 else rng.randint(0, max_start)
            sample.extend(returns[start : start + bl])
        sample = sample[:n]
        ann_ret, sharpe = _ann_ret_and_sharpe(sample, hours_per_period)
        ann_returns.append(ann_ret)
        sharpes.append(sharpe)

    return _bootstrap_ci_from_samples(
        point_ann, point_sharpe, ann_returns, sharpes,
        n_resamples=n_resamples, block_length=bl, method="moving-block",
    )


def stationary_bootstrap(
    returns: Sequence[float],
    *,
    hours_per_period: int,
    block_length: float,
    n_resamples: int = 5000,
    seed: int = 42,
) -> BlockBootstrapStats:
    """Politis-Romano stationary bootstrap.

    Walks the resampled series one position at a time. At each step, draw a
    Bernoulli with probability `p = 1 / block_length` — on success, start a
    new block at a uniformly random position in `[0, n - 1]`; otherwise,
    advance to the next position of the *current* block, wrapping around the
    end of the original series with `(idx + 1) % n`.

    This produces blocks whose lengths follow a geometric distribution with
    mean `block_length`, and the resulting resampled series is strictly
    stationary. Wraparound is intentional here (it's what gives the method
    its stationarity), unlike in the moving-block variant.
    """
    point_ann, point_sharpe = _ann_ret_and_sharpe(returns, hours_per_period)
    n = len(returns)
    if not returns or n_resamples <= 0:
        bl_int = max(1, round(block_length))
        return _bootstrap_ci_from_samples(
            point_ann, point_sharpe, [], [],
            n_resamples=n_resamples, block_length=bl_int,
            method="stationary",
        )

    bl = max(1.0, float(block_length))
    p = 1.0 / bl
    rng = random.Random(seed)

    ann_returns: list[float] = []
    sharpes: list[float] = []
    for _ in range(n_resamples):
        sample: list[float] = []
        idx = rng.randrange(n)
        for _i in range(n):
            sample.append(returns[idx])
            idx = rng.randrange(n) if rng.random() < p else (idx + 1) % n
        ann_ret, sharpe = _ann_ret_and_sharpe(sample, hours_per_period)
        ann_returns.append(ann_ret)
        sharpes.append(sharpe)

    return _bootstrap_ci_from_samples(
        point_ann, point_sharpe, ann_returns, sharpes,
        n_resamples=n_resamples, block_length=max(1, round(bl)),
        method="stationary",
    )
