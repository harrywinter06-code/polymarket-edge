"""Walk-forward (out-of-sample) validation report for the HL funding-capture strategy.

Answers the founder-facing question: "what's the out-of-sample result?"

Headline config: train=15d / test=7d / step=3d, top_k=5, trailing=24h, rebal=8h.
Also runs a few alternative train/test ratios for robustness, then a second
pass net of 5bp/leg (20bp round-trip) for the honest "what survives both" view.

Run via: PYTHONPATH=src python scripts/walk_forward.py
"""

from __future__ import annotations

from datetime import UTC, datetime

from polymarket_edge import db, hl_backtest, walkforward


def _fmt_dt(t_ms: int) -> str:
    return datetime.fromtimestamp(t_ms / 1000.0, tz=UTC).strftime("%m-%d %H:%M")


def _print_window_table(result: walkforward.WalkForwardResult, label: str) -> None:
    print(f"\n=== {label} ===")
    print(
        f"  {'#':>2} {'train':>26} {'test':>26} {'n_te':>5} "
        f"{'IS ann':>8} {'OOS ann':>8} {'IS Sh':>6} {'OOS Sh':>7} "
        f"{'decay_pp':>9} {'carry':>6}"
    )
    for i, w in enumerate(result.windows):
        train_s = f"{_fmt_dt(w.train_start_ms)}->{_fmt_dt(w.train_end_ms)}"
        test_s = f"{_fmt_dt(w.test_start_ms)}->{_fmt_dt(w.test_end_ms)}"
        decay = (w.in_sample_annualized - w.out_of_sample_annualized) * 100
        carry_frac = (
            f"{w.coins_carried_to_test}/{w.coins_held_in_train}"
            if w.coins_held_in_train > 0
            else "0/0"
        )
        print(
            f"  {i:>2} {train_s:>26} {test_s:>26} {w.n_test_periods:>5} "
            f"{w.in_sample_annualized:>+8.4f} {w.out_of_sample_annualized:>+8.4f} "
            f"{w.in_sample_sharpe:>+6.2f} {w.out_of_sample_sharpe:>+7.2f} "
            f"{decay:>+9.2f} {carry_frac:>6}"
        )
    print(
        f"  -> {result.n_windows} windows  |  IS mean ann_ret={result.in_sample_ann_ret_mean:+.4f}"
        f"  OOS mean ann_ret={result.out_of_sample_ann_ret_mean:+.4f}"
        f"  decay={result.is_oos_decay_pp:+.2f}pp"
    )
    print(
        f"     IS std={result.in_sample_ann_ret_std:.4f}  "
        f"OOS std={result.out_of_sample_ann_ret_std:.4f}"
    )


def main() -> None:
    conn = db.connect("polymarket_edge.db")
    ticks = hl_backtest.load_funding(conn)
    print(f"ticks: {len(ticks):,}")
    if not ticks:
        print("no data — aborting")
        return
    t_min = min(t.t_ms for t in ticks)
    t_max = max(t.t_ms for t in ticks)
    span_days = (t_max - t_min) / (1000 * 3600 * 24)
    print(f"span: {_fmt_dt(t_min)} -> {_fmt_dt(t_max)}  ({span_days:.2f} days)")

    # Spec headline: train=15 / test=7 / step=3. With ~20-day common grid,
    # this generates few or zero windows. The script reports it regardless
    # and follows up with smaller train/test splits.
    configs = [
        ("HEADLINE  tr=15 te=7 step=3", 15, 7, 3),
        ("SHORTER   tr=10 te=5 step=3", 10, 5, 3),
        ("SHORTER   tr=10 te=7 step=2", 10, 7, 2),
        ("SHORTEST  tr=7  te=5 step=2", 7, 5, 2),
    ]

    print("\n" + "#" * 78)
    print("# GROSS walk-forward (no execution cost)")
    print("#" * 78)
    for label, tr, te, st in configs:
        r = walkforward.walk_forward_top_k_trailing(
            ticks,
            train_days=tr,
            test_days=te,
            step_days=st,
            top_k=5,
            trailing_hours=24,
            rebalance_hours=8,
        )
        _print_window_table(r, label)

    print("\n" + "#" * 78)
    print("# NET-of-5bp/leg walk-forward (20bp round-trip per rebalance)")
    print("#" * 78)
    for label, tr, te, st in configs:
        r = walkforward.walk_forward_top_k_trailing_net_spread(
            ticks,
            train_days=tr,
            test_days=te,
            step_days=st,
            top_k=5,
            trailing_hours=24,
            rebalance_hours=8,
            spread_bps_per_leg=5.0,
        )
        _print_window_table(r, label + " (net 5bp/leg)")


if __name__ == "__main__":
    main()
