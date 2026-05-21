"""Hyperparameter sensitivity sweep for the HL funding-capture backtest.

Run via: uv run python scripts/sensitivity.py
"""

from __future__ import annotations

from polymarket_edge import db, hl_backtest


def main() -> None:
    conn = db.connect("polymarket_edge.db")
    ticks = hl_backtest.load_funding(conn)
    print(f"ticks: {len(ticks):,}")
    print()
    header = (
        f"{'k':>3} {'trail':>5} {'rebal':>5} {'n':>4} "
        f"{'ann_ret':>9} {'sharpe':>7} {'hit%':>6}"
    )
    print(header)
    for k in (3, 5, 10):
        for trail in (12, 24, 48):
            for rebal in (8, 24):
                r = hl_backtest.backtest_top_k_trailing(
                    ticks, top_k=k, trailing_hours=trail, rebalance_hours=rebal
                )
                print(
                    f"{k:>3} {trail:>5} {rebal:>5} {r.n_rebalances:>4} "
                    f"{r.annualized_return:>+9.4f} {r.sharpe:>+7.2f} "
                    f"{r.hit_rate * 100:>5.1f}%"
                )
    print()
    print("passive baselines (rebal_hours=8):")
    for coin in ("BTC", "ETH", "SOL", "XRP", "DOGE", "LINK", "AVAX", "SUI"):
        r = hl_backtest.backtest_passive(ticks, coin=coin, rebalance_hours=8)
        print(f"  {coin:6} ann_ret={r.annualized_return:+.4f}  sharpe={r.sharpe:+.2f}")


if __name__ == "__main__":
    main()
