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

-- ---------- day 2: forward observation + historical retrospective ----------

CREATE TABLE IF NOT EXISTS signal_trajectories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_run_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    n_markets INTEGER NOT NULL,
    sum_best_bid REAL NOT NULL,
    sum_best_ask REAL NOT NULL,
    bid_gap REAL NOT NULL,
    ask_gap REAL NOT NULL,
    best_gap REAL NOT NULL,
    direction TEXT NOT NULL,
    snapshot_at TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES events(id)
);

CREATE INDEX IF NOT EXISTS idx_traj_run_event ON signal_trajectories(poll_run_id, event_id, snapshot_at);
CREATE INDEX IF NOT EXISTS idx_traj_event_time ON signal_trajectories(event_id, snapshot_at);

CREATE TABLE IF NOT EXISTS prices_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id TEXT NOT NULL,
    market_id TEXT,
    t INTEGER NOT NULL,
    p REAL NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_prices_token_time ON prices_history(token_id, t);

-- ---------- day 3-4: Hyperliquid ----------

CREATE TABLE IF NOT EXISTS hl_universe (
    coin TEXT PRIMARY KEY,
    sz_decimals INTEGER,
    max_leverage INTEGER,
    margin_table_id INTEGER,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hl_funding_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin TEXT NOT NULL,
    funding REAL NOT NULL,             -- hourly funding rate (e.g., 0.0000125 = 0.00125%/hr)
    mark_px REAL,
    mid_px REAL,
    oracle_px REAL,
    premium REAL,
    open_interest REAL,
    day_ntl_vlm REAL,
    snapshot_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hl_fs_coin_time ON hl_funding_snapshots(coin, snapshot_at);

CREATE TABLE IF NOT EXISTS hl_funding_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin TEXT NOT NULL,
    t INTEGER NOT NULL,                -- ms timestamp
    funding REAL NOT NULL,             -- hourly rate
    premium REAL,
    fetched_at TEXT NOT NULL,
    UNIQUE (coin, t)
);

CREATE INDEX IF NOT EXISTS idx_hl_fh_coin_t ON hl_funding_history(coin, t);

-- ---------- day 5: paper-trading ----------

CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    venue TEXT NOT NULL,               -- 'polymarket' | 'hyperliquid'
    event_id TEXT,                     -- polymarket event_id, or NULL for hyperliquid
    coin TEXT,                         -- hyperliquid coin, or NULL for polymarket
    side TEXT NOT NULL,                -- 'buy_yes' / 'sell_yes' / 'long' / 'short'
    notional_usd REAL NOT NULL,
    entry_price REAL,
    entry_gap REAL,                    -- polymarket only: gap at entry
    entry_funding REAL,                -- hyperliquid only: hourly funding at entry
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    realized_pnl_usd REAL,
    close_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_paper_open ON paper_positions(closed_at);

-- ---------- cross-venue case study: PM market vs HL perp ----------

CREATE TABLE IF NOT EXISTS cross_venue_aligned (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pm_token_id TEXT NOT NULL,
    hl_coin TEXT NOT NULL,
    t_ms INTEGER NOT NULL,            -- bucket-start in unix ms
    pm_price REAL NOT NULL,           -- last PM price within bucket [0..1]
    hl_mark REAL NOT NULL,            -- last HL candle close within bucket
    pm_delta REAL NOT NULL,           -- pm_price - prev bucket's pm_price
    hl_log_return REAL NOT NULL,      -- log(hl_mark / prev bucket's hl_mark)
    fetched_at TEXT NOT NULL,
    UNIQUE (pm_token_id, hl_coin, t_ms)
);

CREATE INDEX IF NOT EXISTS idx_cv_pair_time
    ON cross_venue_aligned(pm_token_id, hl_coin, t_ms);

-- ---------- depth-aware trap-rate classification (microstructure scan) ----------

CREATE TABLE IF NOT EXISTS microstructure_classifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id TEXT NOT NULL,             -- one per scan_and_classify run
    event_id TEXT NOT NULL,
    event_slug TEXT,
    event_title TEXT,
    category_tag TEXT NOT NULL,
    n_markets INTEGER NOT NULL,
    neg_risk_augmented INTEGER NOT NULL,
    direction TEXT NOT NULL,           -- 'sell_yes' | 'buy_yes'
    top_of_book_gap REAL NOT NULL,
    gap_at_small_size REAL NOT NULL,
    gap_at_med_size REAL NOT NULL,
    throttle_notional_usd REAL NOT NULL,
    verdict TEXT NOT NULL,             -- 'real' | 'marginal' | 'trap' | 'noise'
    classified_at TEXT NOT NULL
    -- no FK on event_id: this table records research output across scan windows;
    -- the parent events row may not be ingested by the time a scan runs.
);

CREATE INDEX IF NOT EXISTS idx_micro_scan ON microstructure_classifications(scan_id);
CREATE INDEX IF NOT EXISTS idx_micro_verdict_cat
    ON microstructure_classifications(verdict, category_tag);

-- ---------- plan D: hourly perp close prices for funding-extremes study ----------

CREATE TABLE IF NOT EXISTS hl_perp_candles (
    coin TEXT NOT NULL,
    t INTEGER NOT NULL,                -- bucket-start in unix ms (1h candle)
    close REAL NOT NULL,               -- candle close price (mark proxy)
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (coin, t)
);

CREATE INDEX IF NOT EXISTS idx_hl_candles_coin_t ON hl_perp_candles(coin, t);

