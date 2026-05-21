"""SQLite persistence for polymarket-edge."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "schema.sql"


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection, schema_path: Path = SCHEMA_PATH) -> None:
    conn.executescript(schema_path.read_text())
    conn.commit()


def upsert_event(conn: sqlite3.Connection, event: dict[str, Any], fetched_at: str) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO events
        (id, slug, title, neg_risk, neg_risk_augmented, end_date,
         volume, liquidity, n_markets, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(event["id"]),
            event.get("slug"),
            event.get("title"),
            1 if event.get("negRisk") else 0,
            1 if event.get("negRiskAugmented") else 0,
            event.get("endDate"),
            _to_float(event.get("volume")),
            _to_float(event.get("liquidity")),
            len(event.get("markets", [])),
            fetched_at,
        ),
    )


def upsert_market(
    conn: sqlite3.Connection,
    market: dict[str, Any],
    event_id: str,
    fetched_at: str,
) -> None:
    token_ids = _parse_token_ids(market.get("clobTokenIds"))
    yes_id, no_id = ([*token_ids, None, None])[:2]
    conn.execute(
        """
        INSERT OR REPLACE INTO markets
        (id, event_id, question, slug, condition_id, token_yes_id, token_no_id,
         outcomes_json, neg_risk, neg_risk_other, accepting_orders, end_date,
         order_min_size, order_price_min_tick_size, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(market["id"]),
            event_id,
            market.get("question"),
            market.get("slug"),
            market.get("conditionId"),
            yes_id,
            no_id,
            market.get("outcomes"),
            1 if market.get("negRisk") else 0,
            1 if market.get("negRiskOther") else 0,
            1 if market.get("acceptingOrders") else 0,
            market.get("endDate"),
            _to_float(market.get("orderMinSize")),
            _to_float(market.get("orderPriceMinTickSize")),
            fetched_at,
        ),
    )


def insert_market_snapshot(
    conn: sqlite3.Connection,
    market: dict[str, Any],
    snapshot_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO market_snapshots
        (market_id, best_bid, best_ask, spread, last_trade_price,
         outcome_prices_json, volume_num, snapshot_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(market["id"]),
            _to_float(market.get("bestBid")),
            _to_float(market.get("bestAsk")),
            _to_float(market.get("spread")),
            _to_float(market.get("lastTradePrice")),
            market.get("outcomePrices"),
            _to_float(market.get("volumeNum")),
            snapshot_at,
        ),
    )


def insert_arb_signal(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    n_markets: int,
    sum_best_bid: float,
    sum_best_ask: float,
    bid_gap: float,
    ask_gap: float,
    direction: str,
    has_neg_risk_other: bool,
    detected_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO event_arb_signals
        (event_id, n_markets, sum_best_bid, sum_best_ask, bid_gap, ask_gap,
         direction, has_neg_risk_other, detected_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            n_markets,
            sum_best_bid,
            sum_best_ask,
            bid_gap,
            ask_gap,
            direction,
            1 if has_neg_risk_other else 0,
            detected_at,
        ),
    )


def _parse_token_ids(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(t) for t in raw]
    if isinstance(raw, str):
        try:
            return [str(t) for t in json.loads(raw)]
        except json.JSONDecodeError:
            return []
    return []


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
