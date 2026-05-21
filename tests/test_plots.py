"""Tests for the chart-generation module.

Each test seeds a minimal in-tree SQLite DB (or constructs synthetic
dataclasses) and asserts the output file exists and is non-trivial in size
(>1KB). We don't pixel-diff the PNG — these are sanity checks that the
pipeline runs end-to-end on representative data.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from polymarket_edge import db
from polymarket_edge.book_depth import EventDepthResult
from polymarket_edge.plots import (
    plot_depth_decay,
    plot_funding_apr_per_coin,
    plot_hl_cumulative_pnl,
)

MIN_PNG_BYTES = 1024


@pytest.fixture
def tmp_conn(tmp_path: Path) -> sqlite3.Connection:
    p = tmp_path / "test.db"
    conn = db.connect(p)
    db.init_schema(conn)
    return conn


def _seed_funding_history(
    conn: sqlite3.Connection,
    coin_to_hourly: dict[str, list[float]],
    *,
    start_ms: int = 0,
) -> None:
    fetched_at = datetime.now(UTC).isoformat()
    for coin, vals in coin_to_hourly.items():
        for i, f in enumerate(vals):
            conn.execute(
                """INSERT INTO hl_funding_history (coin, t, funding, premium, fetched_at)
                   VALUES (?, ?, ?, NULL, ?)""",
                (coin, start_ms + i * 3_600_000, f, fetched_at),
            )
    conn.commit()


def test_plot_hl_cumulative_pnl_writes_png(tmp_conn: sqlite3.Connection, tmp_path: Path) -> None:
    # 80 hours of data per coin: ample for trailing=24, rebalance=8 (-> 7 rebalances).
    _seed_funding_history(
        tmp_conn,
        {
            "BTC": [0.00005] * 80,
            "ETH": [0.00010] * 80,
            "SOL": [0.00020] * 80,
            "ARB": [0.00030] * 80,
            "OP":  [0.00040] * 80,
            "DOGE": [0.00050] * 80,
        },
    )
    out = tmp_path / "hl_cum.png"
    result = plot_hl_cumulative_pnl(
        tmp_conn, out, top_k=5, trailing_hours=24, rebalance_hours=8
    )
    assert result == out
    assert out.exists()
    assert out.stat().st_size > MIN_PNG_BYTES


def test_plot_hl_cumulative_pnl_empty_db_writes_placeholder(
    tmp_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    out = tmp_path / "hl_cum_empty.png"
    result = plot_hl_cumulative_pnl(tmp_conn, out)
    assert result == out
    assert out.exists()
    assert out.stat().st_size > 0


def test_plot_funding_apr_per_coin_writes_png(
    tmp_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    # Three coins, distinct mean funding rates so the bar order is deterministic.
    # FARTCOIN well above floor, BTC near floor, MEME below floor.
    _seed_funding_history(
        tmp_conn,
        {
            "FARTCOIN": [0.00002] * 48,   # ~17.5% APR
            "BTC":      [0.0000125] * 48, # ~10.95% APR (the floor)
            "MEME":     [0.000005] * 48,  # ~4.4% APR
        },
    )
    out = tmp_path / "apr.png"
    result = plot_funding_apr_per_coin(tmp_conn, out, top_n=10)
    assert result == out
    assert out.exists()
    assert out.stat().st_size > MIN_PNG_BYTES


def test_plot_depth_decay_writes_png(tmp_path: Path) -> None:
    # Synthetic: one event whose gap stays positive, one that collapses below zero
    # as the basket scales (the "Weinstein trap" story).
    world_cup = [
        EventDepthResult(
            notional_per_market_usd=1_000.0, n_markets=12, direction="sell_yes",
            sum_top_of_book=1.015, sum_avg_fill=1.014,
            gap_top_of_book=0.015, gap_depth_aware=0.014,
            basket_throttle_market="Brazil", basket_throttle_notional=1_000.0,
            realized_pnl_per_share=0.014,
        ),
        EventDepthResult(
            notional_per_market_usd=10_000.0, n_markets=12, direction="sell_yes",
            sum_top_of_book=1.015, sum_avg_fill=1.012,
            gap_top_of_book=0.015, gap_depth_aware=0.012,
            basket_throttle_market="Brazil", basket_throttle_notional=10_000.0,
            realized_pnl_per_share=0.012,
        ),
        EventDepthResult(
            notional_per_market_usd=48_000.0, n_markets=12, direction="sell_yes",
            sum_top_of_book=1.015, sum_avg_fill=1.005,
            gap_top_of_book=0.015, gap_depth_aware=0.005,
            basket_throttle_market="Argentina", basket_throttle_notional=20_500.0,
            realized_pnl_per_share=0.005,
        ),
    ]
    weinstein = [
        EventDepthResult(
            notional_per_market_usd=10.0, n_markets=4, direction="sell_yes",
            sum_top_of_book=1.008, sum_avg_fill=1.007,
            gap_top_of_book=0.008, gap_depth_aware=0.007,
            basket_throttle_market="Weinstein-1", basket_throttle_notional=7.83,
            realized_pnl_per_share=0.007,
        ),
        EventDepthResult(
            notional_per_market_usd=50.0, n_markets=4, direction="sell_yes",
            sum_top_of_book=1.008, sum_avg_fill=0.896,
            gap_top_of_book=0.008, gap_depth_aware=-0.104,
            basket_throttle_market="Weinstein-1", basket_throttle_notional=7.83,
            realized_pnl_per_share=-0.104,
        ),
    ]
    out = tmp_path / "depth.png"
    result = plot_depth_decay(
        {"2026 World Cup": world_cup, "Weinstein NYC mayor": weinstein},
        out,
    )
    assert result == out
    assert out.exists()
    assert out.stat().st_size > MIN_PNG_BYTES


def test_plot_depth_decay_empty_writes_placeholder(tmp_path: Path) -> None:
    out = tmp_path / "depth_empty.png"
    result = plot_depth_decay({}, out)
    assert result == out
    assert out.exists()
    assert out.stat().st_size > 0
