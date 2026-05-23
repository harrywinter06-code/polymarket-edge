"""Smoke tests for every Typer command in polymarket_edge.cli.

Each command is invoked through Typer's CliRunner with all network dependencies
monkeypatched. The goal is to pin the public CLI surface — every command must
import, parse args, and exit cleanly on the canned inputs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from polymarket_edge import (
    book_depth,
    cli,
    fetch,
    hyperliquid,
    microstructure,
    monitor,
    paper,
)
from polymarket_edge.book_depth import Level, MarketBook
from polymarket_edge.microstructure import EventClassification

from .conftest import make_event, make_market

runner = CliRunner()


def _wide_sell_event(event_id: str = "E1") -> dict:
    return make_event(
        event_id,
        markets=[
            make_market("m1", best_bid=0.60, best_ask=0.61),
            make_market("m2", best_bid=0.55, best_ask=0.56),
        ],
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def patch_events(monkeypatch: pytest.MonkeyPatch):
    """Provide a default canned list for fetch_all_active_events."""
    default = [_wide_sell_event("E1")]
    state: dict[str, Any] = {"events": default}

    async def fake(**kwargs):
        return state["events"]

    monkeypatch.setattr(fetch, "fetch_all_active_events", fake)
    monkeypatch.setattr(cli.fetch, "fetch_all_active_events", fake)
    monkeypatch.setattr(paper.fetch, "fetch_all_active_events", fake)
    monkeypatch.setattr(monitor.fetch, "fetch_all_active_events", fake)

    return state


def _seed_funding(db_path: Path, *, coins=("BTC", "ETH", "SOL", "ARB", "OP", "DOGE"),
                  n_ticks: int = 200) -> None:
    from polymarket_edge import db as db_module

    conn = db_module.connect(db_path)
    db_module.init_schema(conn)
    for coin in coins:
        for i in range(n_ticks):
            conn.execute(
                """INSERT INTO hl_funding_history (coin, t, funding, premium, fetched_at)
                   VALUES (?, ?, ?, NULL, ?)""",
                (coin, i * 3_600_000, 0.0001, "2026-01-01T00:00:00+00:00"),
            )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Commands that exercise gamma /events
# ---------------------------------------------------------------------------


def test_ingest_command(db_path: Path, patch_events) -> None:
    result = runner.invoke(cli.app, ["ingest", "--db-path", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "fetched 1 events" in result.output


def test_scan_command(db_path: Path, patch_events) -> None:
    result = runner.invoke(
        cli.app, ["scan", "--db-path", str(db_path), "--fee-buffer", "0.0"]
    )
    assert result.exit_code == 0, result.output
    assert "scored" in result.output


def test_scan_command_upserts_events_for_signal_fk(
    db_path: Path, patch_events
) -> None:
    """scan must upsert events itself — without a prior ingest the signal FK
    would otherwise fail."""
    from polymarket_edge import db as db_module

    result = runner.invoke(
        cli.app, ["scan", "--db-path", str(db_path), "--fee-buffer", "0.0"]
    )
    assert result.exit_code == 0, result.output
    conn = db_module.connect(db_path)
    n_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    n_signals = conn.execute("SELECT COUNT(*) FROM event_arb_signals").fetchone()[0]
    conn.close()
    assert n_events >= 1
    assert n_signals >= 1


def test_stats_command_on_empty_db(db_path: Path) -> None:
    result = runner.invoke(cli.app, ["stats", "--db-path", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "events" in result.output


def test_monitor_command(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_monitor(*args, **kwargs):
        return ("test-run", 1, 5)

    monkeypatch.setattr(monitor, "run_monitor", fake_run_monitor)
    monkeypatch.setattr(cli.monitor, "run_monitor", fake_run_monitor)
    result = runner.invoke(
        cli.app,
        ["monitor", "--db-path", str(db_path), "--duration-minutes", "0.001"],
    )
    assert result.exit_code == 0, result.output
    assert "test-run" in result.output
    assert "trajectories_written=5" in result.output


def test_persistence_command_exits_nonzero_with_no_runs(db_path: Path) -> None:
    result = runner.invoke(cli.app, ["persistence", "--db-path", str(db_path)])
    assert result.exit_code == 1
    assert "no poll runs" in result.output.lower()


def test_persistence_command_with_seeded_run(db_path: Path) -> None:
    from polymarket_edge import db as db_module

    conn = db_module.connect(db_path)
    db_module.init_schema(conn)
    conn.execute(
        """INSERT INTO events
           (id, slug, title, neg_risk, neg_risk_augmented, n_markets, fetched_at)
           VALUES ('E1', 'ev', 'ev', 1, 0, 2, 'a')"""
    )
    for i in range(6):
        conn.execute(
            """INSERT INTO signal_trajectories
               (poll_run_id, event_id, n_markets, sum_best_bid, sum_best_ask,
                bid_gap, ask_gap, best_gap, direction, snapshot_at)
               VALUES ('r', 'E1', 2, 1.03, 0.97, 0.03, 0.03, 0.03, 'sell_yes', ?)""",
            (f"2026-01-01T00:0{i}:00+00:00",),
        )
    conn.commit()
    conn.close()
    result = runner.invoke(cli.app, ["persistence", "--db-path", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "snapshots=6" in result.output


def test_runs_command_on_empty(db_path: Path) -> None:
    result = runner.invoke(cli.app, ["runs", "--db-path", str(db_path)])
    assert result.exit_code == 0
    # No runs -> no output rows (just exits cleanly).


def test_depth_command_event_not_found(db_path: Path, patch_events) -> None:
    """Asking for a slug that's not in the fetched events should exit non-zero."""
    result = runner.invoke(cli.app, ["depth", "no-such-slug"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_depth_command_happy_path(
    patch_events, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Depth command on the canned wide-sell event, with mocked /book."""

    async def fake_books(markets, **kwargs):
        out: dict[str, MarketBook] = {}
        for m in markets:
            tokens = m["clobTokenIds"].strip("[]").replace('"', "").split(",")
            yes = tokens[0].strip()
            out[yes] = MarketBook(
                token_id=yes, bids=[Level(0.60, 10_000)], asks=[Level(0.61, 10_000)]
            )
        return out

    monkeypatch.setattr(book_depth, "fetch_books_for_event", fake_books)
    monkeypatch.setattr(cli.book_depth, "fetch_books_for_event", fake_books)
    result = runner.invoke(cli.app, ["depth", "test-event", "--notionals", "10,100"])
    assert result.exit_code == 0, result.output
    assert "event:" in result.output


# ---------------------------------------------------------------------------
# Hyperliquid commands
# ---------------------------------------------------------------------------


def test_hl_ingest_command(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from polymarket_edge import db as db_module

    universe = [{"name": "BTC", "szDecimals": 4, "maxLeverage": 50, "marginTableId": 1}]
    ctxs = [{"funding": "0.0001", "markPx": "60000", "openInterest": "1000"}]

    async def fake(**kwargs):
        return universe, ctxs

    monkeypatch.setattr(hyperliquid, "fetch_meta_and_ctxs", fake)
    monkeypatch.setattr(cli.hyperliquid, "fetch_meta_and_ctxs", fake)
    result = runner.invoke(cli.app, ["hl-ingest", "--db-path", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "snapshot:" in result.output
    # Should have written a time-keyed universe snapshot for survivorship analysis.
    conn = db_module.connect(db_path)
    n_uni = conn.execute(
        "SELECT COUNT(*) FROM hl_universe_snapshots WHERE coin='BTC'"
    ).fetchone()[0]
    conn.close()
    assert n_uni == 1


def test_hl_history_command_explicit_coins(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake(coins, *, days, timeout=30.0):
        return {
            c: [{"coin": c, "fundingRate": "0.0001", "time": 1_700_000_000_000}]
            for c in coins
        }

    monkeypatch.setattr(hyperliquid, "fetch_funding_history_many", fake)
    monkeypatch.setattr(cli.hyperliquid, "fetch_funding_history_many", fake)
    result = runner.invoke(
        cli.app, ["hl-history", "--db-path", str(db_path), "--coins", "btc,eth"]
    )
    assert result.exit_code == 0, result.output
    assert "persisted" in result.output


def test_hl_history_command_falls_back_to_snapshot_oi(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No --coins flag with no snapshot data should exit non-zero."""
    result = runner.invoke(cli.app, ["hl-history", "--db-path", str(db_path)])
    assert result.exit_code == 1
    assert "no snapshot data" in result.output


def test_hl_backtest_command_requires_funding_history(db_path: Path) -> None:
    result = runner.invoke(cli.app, ["hl-backtest", "--db-path", str(db_path)])
    assert result.exit_code == 1
    assert "no funding history" in result.output


def test_hl_backtest_command_happy_path(db_path: Path) -> None:
    _seed_funding(db_path)
    result = runner.invoke(cli.app, ["hl-backtest", "--db-path", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "funding ticks loaded" in result.output


def test_hl_ci_command(db_path: Path) -> None:
    _seed_funding(db_path)
    result = runner.invoke(
        cli.app,
        ["hl-ci", "--db-path", str(db_path), "--n-resamples", "100"],
    )
    assert result.exit_code == 0, result.output
    assert "n_periods=" in result.output


def test_hl_ci_command_no_funding(db_path: Path) -> None:
    result = runner.invoke(cli.app, ["hl-ci", "--db-path", str(db_path)])
    assert result.exit_code == 1


def test_hl_ci_block_command(db_path: Path) -> None:
    _seed_funding(db_path)
    result = runner.invoke(
        cli.app,
        ["hl-ci-block", "--db-path", str(db_path), "--n-resamples", "100"],
    )
    assert result.exit_code == 0, result.output


def test_hl_ci_block_command_no_funding(db_path: Path) -> None:
    result = runner.invoke(cli.app, ["hl-ci-block", "--db-path", str(db_path)])
    assert result.exit_code == 1


def test_hl_cadence_frontier_command(db_path: Path) -> None:
    _seed_funding(db_path, n_ticks=200)
    out_png = db_path.parent / "cadence.png"
    result = runner.invoke(
        cli.app,
        [
            "hl-cadence-frontier",
            "--db-path", str(db_path),
            "--cadences", "8,24,72",
            "--out", str(out_png),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "cadence" in result.output
    assert out_png.exists()


def test_hl_cadence_frontier_command_no_funding(db_path: Path) -> None:
    result = runner.invoke(
        cli.app, ["hl-cadence-frontier", "--db-path", str(db_path)]
    )
    assert result.exit_code == 1


def test_hl_tail_command(db_path: Path) -> None:
    _seed_funding(db_path)
    result = runner.invoke(
        cli.app,
        ["hl-tail", "--db-path", str(db_path), "--spread-bps-per-leg", "5.0"],
    )
    assert result.exit_code == 0, result.output
    assert "VaR_95" in result.output


def test_hl_tail_command_no_funding(db_path: Path) -> None:
    result = runner.invoke(cli.app, ["hl-tail", "--db-path", str(db_path)])
    assert result.exit_code == 1


def test_walk_forward_command(db_path: Path) -> None:
    _seed_funding(db_path, n_ticks=500)
    result = runner.invoke(
        cli.app,
        [
            "walk-forward", "--db-path", str(db_path),
            "--train-days", "5", "--test-days", "2", "--step-days", "1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "strategy:" in result.output


def test_walk_forward_command_no_funding(db_path: Path) -> None:
    result = runner.invoke(cli.app, ["walk-forward", "--db-path", str(db_path)])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Paper / report / dashboard / microstructure / trap-predict
# ---------------------------------------------------------------------------


def test_paper_auto_command(
    db_path: Path, patch_events, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = runner.invoke(
        cli.app,
        ["paper-auto", "--db-path", str(db_path), "--fee-buffer", "0.0"],
    )
    assert result.exit_code == 0, result.output
    assert "opened=" in result.output


def test_paper_pnl_command(db_path: Path) -> None:
    result = runner.invoke(cli.app, ["paper-pnl", "--db-path", str(db_path)])
    assert result.exit_code == 0
    assert "n_open" in result.output


def test_report_command(db_path: Path) -> None:
    out = db_path.parent / "REPORT.md"
    result = runner.invoke(
        cli.app, ["report", "--db-path", str(db_path), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert out.exists()


def test_dashboard_command(db_path: Path) -> None:
    out = db_path.parent / "dashboard.html"
    result = runner.invoke(
        cli.app, ["dashboard", "--db-path", str(db_path), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert out.exists()


def test_microstructure_scan_command(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = [
        EventClassification(
            event_id="E1", event_slug="ev1", event_title="Ev1", category_tag="Sports",
            n_markets=3, neg_risk_augmented=False, top_of_book_gap=0.05,
            direction="sell_yes", gap_at_small_size=0.04, gap_at_med_size=0.03,
            throttle_notional_usd=200.0, verdict="real",
        ),
    ]

    async def fake_scan(**kwargs):
        return sample

    monkeypatch.setattr(microstructure, "scan_and_classify", fake_scan)
    monkeypatch.setattr(cli.microstructure, "scan_and_classify", fake_scan)
    result = runner.invoke(
        cli.app, ["microstructure-scan", "--db-path", str(db_path)]
    )
    assert result.exit_code == 0, result.output
    assert "flagged=1" in result.output


def test_microstructure_scan_command_no_results(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_scan(**kwargs):
        return []

    monkeypatch.setattr(cli.microstructure, "scan_and_classify", fake_scan)
    result = runner.invoke(
        cli.app, ["microstructure-scan", "--db-path", str(db_path)]
    )
    assert result.exit_code == 0
    assert "no flagged events" in result.output


def test_trap_predict_command_requires_classifications(db_path: Path) -> None:
    result = runner.invoke(cli.app, ["trap-predict", "--db-path", str(db_path)])
    assert result.exit_code == 1
    assert "no microstructure_classifications rows" in result.output


def test_trap_predict_command_happy_path(db_path: Path) -> None:
    """Seed enough microstructure_classifications rows for LOOCV to run."""
    from polymarket_edge import db as db_module

    conn = db_module.connect(db_path)
    db_module.init_schema(conn)
    # 20 rows, half traps.
    for i in range(20):
        verdict = "trap" if i < 10 else "real"
        conn.execute(
            """INSERT INTO microstructure_classifications
               (scan_id, event_id, event_slug, event_title, category_tag,
                n_markets, neg_risk_augmented, direction, top_of_book_gap,
                gap_at_small_size, gap_at_med_size, throttle_notional_usd,
                verdict, classified_at)
               VALUES ('scan-1', ?, ?, 'title', 'Politics', ?, 0, 'sell_yes',
                       0.05, ?, ?, 100.0, ?, '2026-01-01')""",
            (f"E{i}", f"ev-{i}", 2 if verdict == "trap" else 5,
             -0.02 if verdict == "trap" else 0.03,
             -0.05 if verdict == "trap" else 0.02, verdict),
        )
    conn.commit()
    conn.close()
    result = runner.invoke(cli.app, ["trap-predict", "--db-path", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "LOOCV AUC" in result.output


def test_cli_help_lists_all_commands() -> None:
    """The CLI registers every advertised command — guard against accidental
    deletion of an entrypoint."""
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    expected = [
        "ingest", "scan", "stats", "monitor", "persistence", "runs",
        "hl-ingest", "hl-history", "hl-backtest", "paper-auto", "paper-pnl",
        "depth", "report", "walk-forward", "hl-ci-block", "microstructure-scan",
        "hl-tail", "trap-predict", "dashboard", "hl-ci",
    ]
    for cmd in expected:
        assert cmd in result.output, f"missing command in --help: {cmd}"
