"""Round-trip tests for the SQLite persistence layer and helpers."""

from __future__ import annotations

import json
import sqlite3

from polymarket_edge import db
from polymarket_edge.db import _parse_token_ids, _to_float

from .conftest import make_event, make_market


def test_to_float_handles_none_numeric_and_invalid() -> None:
    assert _to_float(None) is None
    assert _to_float(0) == 0.0
    assert _to_float("3.5") == 3.5
    assert _to_float("not-a-number") is None
    assert _to_float([1, 2]) is None  # TypeError path


def test_parse_token_ids_accepts_list_json_string_and_rejects_garbage() -> None:
    assert _parse_token_ids(None) == []
    assert _parse_token_ids([1, 2]) == ["1", "2"]
    assert _parse_token_ids('["a","b"]') == ["a", "b"]
    assert _parse_token_ids("not-json") == []
    # Non-list, non-str, non-None values fall through to the empty default.
    assert _parse_token_ids(42) == []


def test_upsert_event_round_trip(tmp_conn: sqlite3.Connection) -> None:
    ev = make_event("E1", title="Trip", slug="trip", neg_risk_augmented=True)
    db.upsert_event(tmp_conn, ev, fetched_at="2026-01-01T00:00:00+00:00")
    row = tmp_conn.execute("SELECT * FROM events WHERE id='E1'").fetchone()
    assert row["title"] == "Trip"
    assert row["slug"] == "trip"
    assert row["neg_risk"] == 1
    assert row["neg_risk_augmented"] == 1
    assert row["n_markets"] == 2
    assert row["volume"] == 100.0


def test_upsert_event_overwrites_existing(tmp_conn: sqlite3.Connection) -> None:
    ev = make_event("E1", title="Original")
    db.upsert_event(tmp_conn, ev, "2026-01-01T00:00:00+00:00")
    ev2 = make_event("E1", title="Updated")
    db.upsert_event(tmp_conn, ev2, "2026-01-02T00:00:00+00:00")
    rows = tmp_conn.execute("SELECT title FROM events WHERE id='E1'").fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "Updated"


def test_upsert_market_parses_token_ids_and_inserts(tmp_conn: sqlite3.Connection) -> None:
    ev = make_event("E1")
    db.upsert_event(tmp_conn, ev, "2026-01-01T00:00:00+00:00")
    m = make_market("M1", yes_token_id="yes-1", no_token_id="no-1")
    db.upsert_market(tmp_conn, m, event_id="E1", fetched_at="2026-01-01T00:00:00+00:00")
    row = tmp_conn.execute("SELECT * FROM markets WHERE id='M1'").fetchone()
    assert row["event_id"] == "E1"
    assert row["token_yes_id"] == "yes-1"
    assert row["token_no_id"] == "no-1"
    assert row["accepting_orders"] == 1


def test_upsert_market_handles_missing_token_ids(tmp_conn: sqlite3.Connection) -> None:
    """Even when clobTokenIds is None we should still insert; yes/no go NULL."""
    ev = make_event("E1")
    db.upsert_event(tmp_conn, ev, "2026-01-01T00:00:00+00:00")
    m = make_market("M2")
    m["clobTokenIds"] = None
    db.upsert_market(tmp_conn, m, event_id="E1", fetched_at="2026-01-01T00:00:00+00:00")
    row = tmp_conn.execute("SELECT * FROM markets WHERE id='M2'").fetchone()
    assert row["token_yes_id"] is None
    assert row["token_no_id"] is None


def test_insert_market_snapshot_records_quote_state(tmp_conn: sqlite3.Connection) -> None:
    ev = make_event("E1")
    db.upsert_event(tmp_conn, ev, "2026-01-01T00:00:00+00:00")
    m = make_market("M1", best_bid=0.55, best_ask=0.57)
    db.upsert_market(tmp_conn, m, event_id="E1", fetched_at="2026-01-01T00:00:00+00:00")
    db.insert_market_snapshot(tmp_conn, m, snapshot_at="2026-01-01T00:00:00+00:00")
    row = tmp_conn.execute("SELECT * FROM market_snapshots WHERE market_id='M1'").fetchone()
    assert row["best_bid"] == 0.55
    assert row["best_ask"] == 0.57
    assert row["volume_num"] == 1000.0


def test_insert_arb_signal_round_trip(tmp_conn: sqlite3.Connection) -> None:
    ev = make_event("E1")
    db.upsert_event(tmp_conn, ev, "2026-01-01T00:00:00+00:00")
    db.insert_arb_signal(
        tmp_conn,
        event_id="E1",
        n_markets=2,
        sum_best_bid=1.05,
        sum_best_ask=0.92,
        bid_gap=0.05,
        ask_gap=0.08,
        direction="both",
        has_neg_risk_other=True,
        detected_at="2026-01-01T00:00:00+00:00",
    )
    row = tmp_conn.execute("SELECT * FROM event_arb_signals WHERE event_id='E1'").fetchone()
    assert row["bid_gap"] == 0.05
    assert row["ask_gap"] == 0.08
    assert row["direction"] == "both"
    assert row["has_neg_risk_other"] == 1
    assert row["n_markets"] == 2


def test_init_schema_idempotent(tmp_conn: sqlite3.Connection) -> None:
    """Calling init_schema twice must not raise (CREATE TABLE IF NOT EXISTS)."""
    db.init_schema(tmp_conn)
    db.init_schema(tmp_conn)
    # Smoke: the schema is still queryable.
    tables = {
        r[0]
        for r in tmp_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "events" in tables
    assert "event_arb_signals" in tables
    assert "paper_positions" in tables


def test_upsert_market_round_trips_json_outcomes(tmp_conn: sqlite3.Connection) -> None:
    ev = make_event("E1")
    db.upsert_event(tmp_conn, ev, "2026-01-01T00:00:00+00:00")
    m = make_market("M1")
    m["outcomes"] = json.dumps(["Yes", "No"])
    db.upsert_market(tmp_conn, m, event_id="E1", fetched_at="2026-01-01T00:00:00+00:00")
    row = tmp_conn.execute("SELECT outcomes_json FROM markets WHERE id='M1'").fetchone()
    assert json.loads(row["outcomes_json"]) == ["Yes", "No"]


def test_insert_book_snapshot_persists_levels_and_half_spread(
    tmp_conn: sqlite3.Connection,
) -> None:
    db.insert_book_snapshot(
        tmp_conn,
        token_id="yes-1",
        market_id="M1",
        event_id="E1",
        snapshot_at="2026-01-01T00:00:00+00:00",
        bids=[(0.55, 100.0), (0.54, 50.0), (0.53, 25.0)],
        asks=[(0.57, 200.0), (0.58, 150.0)],
    )
    row = tmp_conn.execute(
        "SELECT * FROM book_snapshots WHERE token_id='yes-1'"
    ).fetchone()
    assert row["best_bid_price"] == 0.55
    assert row["best_ask_price"] == 0.57
    assert abs(row["half_spread"] - 0.01) < 1e-12
    assert json.loads(row["bid_levels_json"]) == [
        [0.55, 100.0], [0.54, 50.0], [0.53, 25.0]
    ]


def test_insert_book_snapshot_handles_empty_side(
    tmp_conn: sqlite3.Connection,
) -> None:
    db.insert_book_snapshot(
        tmp_conn,
        token_id="yes-1",
        market_id=None,
        event_id=None,
        snapshot_at="t",
        bids=[(0.55, 100.0)],
        asks=[],
    )
    row = tmp_conn.execute("SELECT * FROM book_snapshots").fetchone()
    assert row["best_bid_price"] == 0.55
    assert row["best_ask_price"] is None
    assert row["half_spread"] is None


def test_insert_hl_universe_snapshot_unique_per_coin_time(
    tmp_conn: sqlite3.Connection,
) -> None:
    db.insert_hl_universe_snapshot(
        tmp_conn, coin="BTC", sz_decimals=4, max_leverage=50,
        open_interest=1000.0, snapshot_at="t1",
    )
    # Same (coin, snapshot_at) is silently ignored by INSERT OR IGNORE.
    db.insert_hl_universe_snapshot(
        tmp_conn, coin="BTC", sz_decimals=4, max_leverage=50,
        open_interest=2000.0, snapshot_at="t1",
    )
    # Different snapshot_at: new row.
    db.insert_hl_universe_snapshot(
        tmp_conn, coin="BTC", sz_decimals=4, max_leverage=50,
        open_interest=1500.0, snapshot_at="t2",
    )
    rows = tmp_conn.execute(
        "SELECT snapshot_at, open_interest FROM hl_universe_snapshots "
        "WHERE coin='BTC' ORDER BY snapshot_at"
    ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [("t1", 1000.0), ("t2", 1500.0)]
