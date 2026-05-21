CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    slug TEXT,
    title TEXT,
    neg_risk INTEGER NOT NULL,
    neg_risk_augmented INTEGER NOT NULL,
    end_date TEXT,
    volume REAL,
    liquidity REAL,
    n_markets INTEGER,
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_neg_risk ON events(neg_risk);

CREATE TABLE IF NOT EXISTS markets (
    id TEXT PRIMARY KEY,
    event_id TEXT,
    question TEXT,
    slug TEXT,
    condition_id TEXT,
    token_yes_id TEXT,
    token_no_id TEXT,
    outcomes_json TEXT,
    neg_risk INTEGER NOT NULL,
    neg_risk_other INTEGER,
    accepting_orders INTEGER,
    end_date TEXT,
    order_min_size REAL,
    order_price_min_tick_size REAL,
    fetched_at TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES events(id)
);

CREATE INDEX IF NOT EXISTS idx_markets_event ON markets(event_id);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    best_bid REAL,
    best_ask REAL,
    spread REAL,
    last_trade_price REAL,
    outcome_prices_json TEXT,
    volume_num REAL,
    snapshot_at TEXT NOT NULL,
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_market_time ON market_snapshots(market_id, snapshot_at);

CREATE TABLE IF NOT EXISTS event_arb_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    n_markets INTEGER NOT NULL,
    sum_best_bid REAL NOT NULL,
    sum_best_ask REAL NOT NULL,
    bid_gap REAL NOT NULL,
    ask_gap REAL NOT NULL,
    direction TEXT NOT NULL,
    has_neg_risk_other INTEGER NOT NULL,
    detected_at TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES events(id)
);

CREATE INDEX IF NOT EXISTS idx_signals_detected ON event_arb_signals(detected_at);
