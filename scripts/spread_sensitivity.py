"""Spread-cost sensitivity sweep for the Hyperliquid funding-capture strategy.

The headline gross return (+19% annualized at 8h rebalance) ignores the
execution cost of the hedge leg. This script quantifies how the result changes
under realistic spread assumptions.

Run via: PYTHONPATH=src python scripts/spread_sensitivity.py
"""

from __future__ import annotations

from polymarket_edge import db, hl_backtest, hl_hedge


def main() -> None:
    conn = db.connect("polymarket_edge.db")
    ticks = hl_backtest.load_funding(conn)
    print(f"ticks: {len(ticks):,}\n")

    print("Rebalance-period sensitivity at default 5bp/leg (20bp round-trip):")
    print(f"{'rebal_h':>7} {'n':>4} {'gross_ann':>10} {'net_ann':>9} {'sharpe':>7}")
    for rebal in (8, 24, 72, 168, 336):
        gross = hl_backtest.backtest_top_k_trailing(
            ticks, top_k=5, trailing_hours=24, rebalance_hours=rebal
        )
        net = hl_hedge.backtest_top_k_trailing_net_spread(
            ticks,
            top_k=5,
            trailing_hours=24,
            rebalance_hours=rebal,
            spread_bps_per_leg=5.0,
        )
        print(
            f"{rebal:>7} {net.n_rebalances:>4} "
            f"{gross.annualized_return:>+10.4f} "
            f"{net.annualized_return:>+9.4f} {net.sharpe:>+7.2f}"
        )

    print("\nAt rebal_hours=168 (weekly), spread sweep:")
    sweep = hl_hedge.sweep_spread_sensitivity(
        ticks,
        top_k=5,
        trailing_hours=168,
        rebalance_hours=168,
        spreads_bps=(0.0, 1.0, 2.5, 5.0, 10.0),
    )
    for r in sweep:
        spread = r.strategy.split("_spread")[-1] if "_spread" in r.strategy else "?"
        print(
            f"  spread={spread:>10}  ann_ret={r.annualized_return:>+8.4f}  "
            f"sharpe={r.sharpe:>+6.2f}"
        )


if __name__ == "__main__":
    main()
