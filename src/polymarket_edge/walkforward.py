"""Walk-forward (out-of-sample) validation for the HL funding-capture strategy.

The headline +19% annualized in `hl_backtest.backtest_top_k_trailing` is computed
on the same 30-day window for both predictor estimation and realization — pure
in-sample. REDTEAM item 3d touches on a related survivorship effect; this module
addresses the bigger statistical-honesty gap: there is no out-of-sample number.

The trailing-mean predictor has no fit hyperparameters (the trailing length
itself is a config). The fair IS-vs-OOS comparison for a no-hyperparameter
strategy is:
  - IS:  run the predictor + realizer over the full window (train + test).
  - OOS: run the predictor + realizer ONLY over the test segment, where each
         test-period rebalance uses trailing data ending at the rebalance
         start time. The trailing window is allowed to dip into train (a
         strategy in production reads the most recent N hours regardless of
         calendar partition), but realized returns count only on the test
         segment.

Sliding scheme:
  - Window 0: train days [0, train_days), test days [train_days, train_days+test_days).
  - Window k: both shifted forward by step_days.
  - Stop when test_end exceeds total data.

No look-ahead: at each test-period rebalance, the trailing predictor reads
only the `trailing_hours` of grid ticks ending at the rebalance start. The
realizer reads only the `rebalance_hours` after that. Identical guard
structure to `hl_backtest.backtest_top_k_trailing`, scoped to the test segment.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from polymarket_edge.hl_backtest import (
    HOURS_PER_YEAR,
    FundingTick,
    backtest_top_k_trailing,
)

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS


@dataclass(frozen=True, slots=True)
class Window:
    train_start_ms: int
    train_end_ms: int
    test_start_ms: int
    test_end_ms: int
    n_train_periods: int
    n_test_periods: int
    in_sample_annualized: float
    out_of_sample_annualized: float
    in_sample_sharpe: float
    out_of_sample_sharpe: float
    coins_held_in_train: int
    coins_carried_to_test: int


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    strategy: str
    n_windows: int
    in_sample_ann_ret_mean: float
    out_of_sample_ann_ret_mean: float
    in_sample_ann_ret_std: float
    out_of_sample_ann_ret_std: float
    is_oos_decay_pp: float
    windows: list[Window]


def _series_by_coin(ticks: Sequence[FundingTick]) -> dict[str, list[FundingTick]]:
    out: dict[str, list[FundingTick]] = {}
    for t in ticks:
        out.setdefault(t.coin, []).append(t)
    for k in out:
        out[k].sort(key=lambda x: x.t_ms)
    return out


def _union_grid(per_coin: dict[str, list[FundingTick]]) -> list[int]:
    """Survivorship-aware grid (union of timestamps)."""
    if not per_coin:
        return []
    out: set[int] = set()
    for series in per_coin.values():
        out.update(t.t_ms for t in series)
    return sorted(out)


def _maps(per_coin: dict[str, list[FundingTick]]) -> dict[str, dict[int, float]]:
    return {c: {t.t_ms: t.funding for t in series} for c, series in per_coin.items()}


def _annualize(per_period_return: float, hours_per_period: int) -> float:
    periods_per_year = HOURS_PER_YEAR / hours_per_period
    return per_period_return * periods_per_year


def _annualize_vol(per_period_std: float, hours_per_period: int) -> float:
    periods_per_year = HOURS_PER_YEAR / hours_per_period
    return per_period_std * (periods_per_year ** 0.5)


def _ann_ret_and_sharpe(
    returns: Sequence[float], hours_per_period: int
) -> tuple[float, float]:
    if not returns:
        return 0.0, 0.0
    mean = statistics.fmean(returns)
    std = statistics.pstdev(returns) if len(returns) >= 2 else 0.0
    ann_ret = _annualize(mean, hours_per_period)
    ann_vol = _annualize_vol(std, hours_per_period)
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
    return ann_ret, sharpe


def _run_segment(
    *,
    maps: dict[str, dict[int, float]],
    grid: list[int],
    realize_start_idx: int,
    realize_end_idx: int,
    top_k: int,
    trailing_hours: int,
    rebalance_hours: int,
) -> tuple[list[float], set[str]]:
    """Run the trailing-mean strategy with realization restricted to a slice
    of the grid `[realize_start_idx, realize_end_idx)`. The trailing predictor
    at each rebalance reads `grid[i - trailing_hours : i]` regardless of which
    side of the slice boundary it crosses — production reads the most recent
    N hours of funding, not a calendar partition.

    Returns the per-period returns list and the set of coins held at any
    rebalance.
    """
    returns: list[float] = []
    coins_held: set[str] = set()
    i = max(realize_start_idx, trailing_hours)
    while i + rebalance_hours <= realize_end_idx:
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
            returns.append(total_short_pnl / per_coin_count)
        i += rebalance_hours
    return returns, coins_held


def _ticks_in_range(
    ticks: Sequence[FundingTick], start_ms: int, end_ms: int
) -> list[FundingTick]:
    return [t for t in ticks if start_ms <= t.t_ms < end_ms]


def walk_forward_top_k_trailing(
    ticks: Sequence[FundingTick],
    *,
    train_days: int = 15,
    test_days: int = 7,
    step_days: int = 3,
    top_k: int = 5,
    trailing_hours: int = 24,
    rebalance_hours: int = 8,
) -> WalkForwardResult:
    """Sliding-window walk-forward validation.

    For each window:
      - IS: run `backtest_top_k_trailing` on the (train+test) slice. This is
        the existing in-sample number scoped to the window.
      - OOS: run the same predictor + realizer with realization restricted to
        the test segment. Trailing data may dip into the train segment for
        the lookback — production reads the most recent N hours regardless
        of any calendar partition. The test catches the look-ahead bug
        (trailing data leaking from the FUTURE into the predictor).

    Stops sliding when the next test segment would extend past the available
    common-grid span.
    """
    if step_days <= 0:
        raise ValueError(f"step_days must be > 0, got {step_days}")
    if train_days <= 0:
        raise ValueError(f"train_days must be > 0, got {train_days}")
    if test_days <= 0:
        raise ValueError(f"test_days must be > 0, got {test_days}")

    strategy = (
        f"walkforward_top{top_k}_trail{trailing_hours}h_rebal{rebalance_hours}h"
        f"_tr{train_days}d_te{test_days}d_step{step_days}d"
    )

    if not ticks:
        return WalkForwardResult(
            strategy=strategy,
            n_windows=0,
            in_sample_ann_ret_mean=0.0,
            out_of_sample_ann_ret_mean=0.0,
            in_sample_ann_ret_std=0.0,
            out_of_sample_ann_ret_std=0.0,
            is_oos_decay_pp=0.0,
            windows=[],
        )

    per_coin = _series_by_coin(ticks)
    grid = _union_grid(per_coin)
    if not grid:
        return WalkForwardResult(
            strategy=strategy,
            n_windows=0,
            in_sample_ann_ret_mean=0.0,
            out_of_sample_ann_ret_mean=0.0,
            in_sample_ann_ret_std=0.0,
            out_of_sample_ann_ret_std=0.0,
            is_oos_decay_pp=0.0,
            windows=[],
        )
    maps = _maps(per_coin)

    data_start_ms = grid[0]
    data_end_ms = grid[-1] + HOUR_MS  # exclusive end of last hourly bucket
    total_span_ms = data_end_ms - data_start_ms
    needed_span_ms = (train_days + test_days) * DAY_MS

    if total_span_ms < needed_span_ms:
        return WalkForwardResult(
            strategy=strategy,
            n_windows=0,
            in_sample_ann_ret_mean=0.0,
            out_of_sample_ann_ret_mean=0.0,
            in_sample_ann_ret_std=0.0,
            out_of_sample_ann_ret_std=0.0,
            is_oos_decay_pp=0.0,
            windows=[],
        )

    windows: list[Window] = []
    offset_ms = 0
    step_ms = step_days * DAY_MS
    train_span_ms = train_days * DAY_MS
    test_span_ms = test_days * DAY_MS

    while data_start_ms + offset_ms + train_span_ms + test_span_ms <= data_end_ms:
        train_start = data_start_ms + offset_ms
        train_end = train_start + train_span_ms
        test_start = train_end
        test_end = test_start + test_span_ms

        # IS: the existing backtest run over the full (train+test) slice.
        is_ticks = _ticks_in_range(ticks, train_start, test_end)
        is_result = backtest_top_k_trailing(
            is_ticks,
            top_k=top_k,
            trailing_hours=trailing_hours,
            rebalance_hours=rebalance_hours,
        )

        # OOS: realization restricted to the test segment using the full-data
        # grid (so the trailing window can read into train). Find the test-
        # segment slice of the global grid.
        try:
            test_first_idx = next(i for i, t in enumerate(grid) if t >= test_start)
        except StopIteration:
            test_first_idx = len(grid)
        try:
            test_last_idx = next(i for i, t in enumerate(grid) if t >= test_end)
        except StopIteration:
            test_last_idx = len(grid)
        oos_returns, _oos_coins = _run_segment(
            maps=maps,
            grid=grid,
            realize_start_idx=test_first_idx,
            realize_end_idx=test_last_idx,
            top_k=top_k,
            trailing_hours=trailing_hours,
            rebalance_hours=rebalance_hours,
        )
        oos_ann, oos_sharpe = _ann_ret_and_sharpe(oos_returns, rebalance_hours)

        # "coins still in the universe in test": the test-segment universe is
        # whatever coins have a full hourly tick set in the test span. Re-run
        # the predictor on the train segment alone to get the coin SET (the
        # train_result aggregate only reports a count, not the members).
        test_universe_ticks = _ticks_in_range(ticks, test_start, test_end)
        test_universe_coins = set(_series_by_coin(test_universe_ticks).keys())
        try:
            train_first_idx = next(i for i, t in enumerate(grid) if t >= train_start)
        except StopIteration:
            train_first_idx = len(grid)
        try:
            train_last_idx = next(i for i, t in enumerate(grid) if t >= train_end)
        except StopIteration:
            train_last_idx = len(grid)
        train_returns, train_coins = _run_segment(
            maps=maps,
            grid=grid,
            realize_start_idx=train_first_idx,
            realize_end_idx=train_last_idx,
            top_k=top_k,
            trailing_hours=trailing_hours,
            rebalance_hours=rebalance_hours,
        )
        carried = len(train_coins & test_universe_coins)

        windows.append(
            Window(
                train_start_ms=train_start,
                train_end_ms=train_end,
                test_start_ms=test_start,
                test_end_ms=test_end,
                n_train_periods=len(train_returns),
                n_test_periods=len(oos_returns),
                in_sample_annualized=is_result.annualized_return,
                out_of_sample_annualized=oos_ann,
                in_sample_sharpe=is_result.sharpe,
                out_of_sample_sharpe=oos_sharpe,
                coins_held_in_train=len(train_coins),
                coins_carried_to_test=carried,
            )
        )

        offset_ms += step_ms

    if not windows:
        return WalkForwardResult(
            strategy=strategy,
            n_windows=0,
            in_sample_ann_ret_mean=0.0,
            out_of_sample_ann_ret_mean=0.0,
            in_sample_ann_ret_std=0.0,
            out_of_sample_ann_ret_std=0.0,
            is_oos_decay_pp=0.0,
            windows=[],
        )

    is_values = [w.in_sample_annualized for w in windows]
    oos_values = [w.out_of_sample_annualized for w in windows]
    is_mean = statistics.fmean(is_values)
    oos_mean = statistics.fmean(oos_values)
    is_std = statistics.pstdev(is_values) if len(is_values) >= 2 else 0.0
    oos_std = statistics.pstdev(oos_values) if len(oos_values) >= 2 else 0.0
    decay_pp = (is_mean - oos_mean) * 100.0

    return WalkForwardResult(
        strategy=strategy,
        n_windows=len(windows),
        in_sample_ann_ret_mean=is_mean,
        out_of_sample_ann_ret_mean=oos_mean,
        in_sample_ann_ret_std=is_std,
        out_of_sample_ann_ret_std=oos_std,
        is_oos_decay_pp=decay_pp,
        windows=windows,
    )


def walk_forward_top_k_trailing_net_spread(
    ticks: Sequence[FundingTick],
    *,
    train_days: int = 15,
    test_days: int = 7,
    step_days: int = 3,
    top_k: int = 5,
    trailing_hours: int = 24,
    rebalance_hours: int = 8,
    spread_bps_per_leg: float = 5.0,
) -> WalkForwardResult:
    """Walk-forward with the round-trip spread cost subtracted from every
    realized rebalance return. Same windowing as the gross variant; the cost
    is `4 * spread_bps_per_leg / 10_000` per rebalance, applied symmetrically
    to IS and OOS so the decay number reflects net carry only.
    """
    if step_days <= 0:
        raise ValueError(f"step_days must be > 0, got {step_days}")
    if train_days <= 0:
        raise ValueError(f"train_days must be > 0, got {train_days}")
    if test_days <= 0:
        raise ValueError(f"test_days must be > 0, got {test_days}")

    cost = 4 * spread_bps_per_leg / 10_000
    strategy = (
        f"walkforward_top{top_k}_trail{trailing_hours}h_rebal{rebalance_hours}h"
        f"_tr{train_days}d_te{test_days}d_step{step_days}d"
        f"_spread{spread_bps_per_leg}bp"
    )

    if not ticks:
        return WalkForwardResult(
            strategy=strategy, n_windows=0,
            in_sample_ann_ret_mean=0.0, out_of_sample_ann_ret_mean=0.0,
            in_sample_ann_ret_std=0.0, out_of_sample_ann_ret_std=0.0,
            is_oos_decay_pp=0.0, windows=[],
        )

    per_coin = _series_by_coin(ticks)
    grid = _union_grid(per_coin)
    if not grid:
        return WalkForwardResult(
            strategy=strategy, n_windows=0,
            in_sample_ann_ret_mean=0.0, out_of_sample_ann_ret_mean=0.0,
            in_sample_ann_ret_std=0.0, out_of_sample_ann_ret_std=0.0,
            is_oos_decay_pp=0.0, windows=[],
        )
    maps = _maps(per_coin)
    data_start_ms = grid[0]
    data_end_ms = grid[-1] + HOUR_MS
    needed = (train_days + test_days) * DAY_MS
    if data_end_ms - data_start_ms < needed:
        return WalkForwardResult(
            strategy=strategy, n_windows=0,
            in_sample_ann_ret_mean=0.0, out_of_sample_ann_ret_mean=0.0,
            in_sample_ann_ret_std=0.0, out_of_sample_ann_ret_std=0.0,
            is_oos_decay_pp=0.0, windows=[],
        )

    windows: list[Window] = []
    offset_ms = 0
    step_ms = step_days * DAY_MS
    train_span_ms = train_days * DAY_MS
    test_span_ms = test_days * DAY_MS

    while data_start_ms + offset_ms + train_span_ms + test_span_ms <= data_end_ms:
        train_start = data_start_ms + offset_ms
        train_end = train_start + train_span_ms
        test_start = train_end
        test_end = test_start + test_span_ms

        # IS net: run full-window segment, subtract cost per rebalance.
        try:
            is_first = next(i for i, t in enumerate(grid) if t >= train_start)
        except StopIteration:
            is_first = len(grid)
        try:
            is_last = next(i for i, t in enumerate(grid) if t >= test_end)
        except StopIteration:
            is_last = len(grid)
        is_returns, _is_coins = _run_segment(
            maps=maps, grid=grid,
            realize_start_idx=is_first, realize_end_idx=is_last,
            top_k=top_k, trailing_hours=trailing_hours,
            rebalance_hours=rebalance_hours,
        )
        is_net = [r - cost for r in is_returns]
        is_ann, is_sharpe = _ann_ret_and_sharpe(is_net, rebalance_hours)

        try:
            test_first = next(i for i, t in enumerate(grid) if t >= test_start)
        except StopIteration:
            test_first = len(grid)
        try:
            test_last = next(i for i, t in enumerate(grid) if t >= test_end)
        except StopIteration:
            test_last = len(grid)
        oos_returns, _oos_coins = _run_segment(
            maps=maps, grid=grid,
            realize_start_idx=test_first, realize_end_idx=test_last,
            top_k=top_k, trailing_hours=trailing_hours,
            rebalance_hours=rebalance_hours,
        )
        oos_net = [r - cost for r in oos_returns]
        oos_ann, oos_sharpe = _ann_ret_and_sharpe(oos_net, rebalance_hours)

        # Coin carry: train-only segment vs test universe.
        try:
            train_first = next(i for i, t in enumerate(grid) if t >= train_start)
        except StopIteration:
            train_first = len(grid)
        try:
            train_last = next(i for i, t in enumerate(grid) if t >= train_end)
        except StopIteration:
            train_last = len(grid)
        train_returns, train_coins = _run_segment(
            maps=maps, grid=grid,
            realize_start_idx=train_first, realize_end_idx=train_last,
            top_k=top_k, trailing_hours=trailing_hours,
            rebalance_hours=rebalance_hours,
        )
        test_universe_ticks = _ticks_in_range(ticks, test_start, test_end)
        test_universe_coins = set(_series_by_coin(test_universe_ticks).keys())
        carried = len(train_coins & test_universe_coins)

        windows.append(
            Window(
                train_start_ms=train_start,
                train_end_ms=train_end,
                test_start_ms=test_start,
                test_end_ms=test_end,
                n_train_periods=len(train_returns),
                n_test_periods=len(oos_net),
                in_sample_annualized=is_ann,
                out_of_sample_annualized=oos_ann,
                in_sample_sharpe=is_sharpe,
                out_of_sample_sharpe=oos_sharpe,
                coins_held_in_train=len(train_coins),
                coins_carried_to_test=carried,
            )
        )

        offset_ms += step_ms

    if not windows:
        return WalkForwardResult(
            strategy=strategy, n_windows=0,
            in_sample_ann_ret_mean=0.0, out_of_sample_ann_ret_mean=0.0,
            in_sample_ann_ret_std=0.0, out_of_sample_ann_ret_std=0.0,
            is_oos_decay_pp=0.0, windows=[],
        )

    is_values = [w.in_sample_annualized for w in windows]
    oos_values = [w.out_of_sample_annualized for w in windows]
    is_mean = statistics.fmean(is_values)
    oos_mean = statistics.fmean(oos_values)
    is_std = statistics.pstdev(is_values) if len(is_values) >= 2 else 0.0
    oos_std = statistics.pstdev(oos_values) if len(oos_values) >= 2 else 0.0
    decay_pp = (is_mean - oos_mean) * 100.0
    return WalkForwardResult(
        strategy=strategy,
        n_windows=len(windows),
        in_sample_ann_ret_mean=is_mean,
        out_of_sample_ann_ret_mean=oos_mean,
        in_sample_ann_ret_std=is_std,
        out_of_sample_ann_ret_std=oos_std,
        is_oos_decay_pp=decay_pp,
        windows=windows,
    )
