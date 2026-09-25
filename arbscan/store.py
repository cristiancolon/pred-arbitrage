"""SQLite storage. One file holds the market catalog, matcher output, review
decisions, and everything the scanner records."""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    venue TEXT NOT NULL,            -- 'K' (Kalshi) or 'P' (Polymarket US)
    id TEXT NOT NULL,               -- Kalshi market ticker / Polymarket US slug
    event_id TEXT,
    series TEXT,
    category TEXT,
    title TEXT,
    yes_label TEXT,
    no_label TEXT,
    market_type TEXT,
    start_ts REAL,                  -- game start, when the venue gives one
    close_ts REAL,                  -- expected resolution / end date
    rules TEXT,
    fee_coef REAL,
    yes_bid REAL,
    yes_ask REAL,
    volume REAL,
    updated REAL,
    PRIMARY KEY (venue, id)
);

CREATE TABLE IF NOT EXISTS candidates (
    kalshi TEXT NOT NULL,
    pm TEXT NOT NULL,
    score REAL NOT NULL,
    relation TEXT NOT NULL,         -- 'same' | 'inverse'
    confident INTEGER NOT NULL,     -- relation inferred from matching outcome labels
    created REAL NOT NULL,
    PRIMARY KEY (kalshi, pm)
);

CREATE TABLE IF NOT EXISTS decisions (
    kalshi TEXT NOT NULL,
    pm TEXT NOT NULL,
    decision TEXT NOT NULL,         -- 'same' | 'inverse' | 'reject'
    ts REAL NOT NULL,
    source TEXT,                    -- 'human' | 'rule' (auto_approve) | 'jev'
    PRIMARY KEY (kalshi, pm)
);

-- Jev's verdict on each candidate it has read (see jev.py). Kept across refreshes
-- so a pair is only sent again when the matcher's relation or the markets' text
-- changes.
CREATE TABLE IF NOT EXISTS jev_reviews (
    kalshi TEXT NOT NULL,
    pm TEXT NOT NULL,
    relation TEXT NOT NULL,         -- the relation the matcher proposed
    input_hash TEXT NOT NULL,       -- hash of the request; re-asked when it changes
    model TEXT,                     -- the versioned model that answered
    verdict TEXT NOT NULL,          -- 'approve' | 'reject' | 'unsure'
    reason TEXT,
    answers TEXT,                   -- JSON: side probabilities and yes/no values
    tokens INTEGER,
    ts REAL NOT NULL,
    PRIMARY KEY (kalshi, pm)
);

-- Top of book per pair, written only when something changes.
CREATE TABLE IF NOT EXISTS quotes (
    ts REAL NOT NULL,
    pair TEXT NOT NULL,
    k_yes_ask REAL, k_yes_sz REAL,
    k_no_ask REAL, k_no_sz REAL,
    p_yes_bid REAL, p_yes_ask REAL,
    edge_a REAL,                    -- net top-of-book edge, first direction ($/pair)
    edge_b REAL                     -- second direction
);
CREATE INDEX IF NOT EXISTS quotes_pair_ts ON quotes (pair, ts);

-- Depth-walked observations with positive profit after fees.
CREATE TABLE IF NOT EXISTS opportunities (
    ts REAL NOT NULL,
    pair TEXT NOT NULL,
    direction TEXT NOT NULL,
    top_edge REAL,
    size INTEGER,
    cost REAL,
    profit REAL,
    last_edge REAL,
    days_to_resolve REAL,
    k_book TEXT,                    -- JSON [[price, qty], ...] of the Kalshi leg
    p_book TEXT                     -- same for the Polymarket US leg
);
CREATE INDEX IF NOT EXISTS opps_ts ON opportunities (ts);

-- Contiguous runs of positive observations for one pair + direction.
CREATE TABLE IF NOT EXISTS episodes (
    pair TEXT NOT NULL,
    direction TEXT NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    n_obs INTEGER NOT NULL,
    max_top_edge REAL,
    max_profit REAL,
    max_size INTEGER,
    first_profit REAL,
    cost_at_max REAL,               -- capital needed for max_profit
    days_to_resolve REAL
);
CREATE INDEX IF NOT EXISTS episodes_start ON episodes (start_ts);

CREATE TABLE IF NOT EXISTS sweeps (
    ts REAL NOT NULL,
    n_pairs INTEGER,
    dur_ms INTEGER,
    depth_fetches INTEGER,
    errors INTEGER,
    best_edge REAL,                 -- best top-of-book net edge of any pair this sweep
    best_pair TEXT,
    best_dir TEXT
);
CREATE INDEX IF NOT EXISTS sweeps_ts ON sweeps (ts);
"""


# Columns added after the first release, so older databases can be upgraded in place.
MIGRATIONS = {
    "decisions": [("source", "TEXT")],
    "episodes": [("cost_at_max", "REAL")],
    "sweeps": [("best_edge", "REAL"), ("best_pair", "TEXT"), ("best_dir", "TEXT")],
}


def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    # NORMAL is safe with WAL (no corruption, may lose the last commit on power loss)
    # and fsyncs far less, which matters on an SD card.
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        for name, kind in cols:
            if name not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    db.commit()
    return db


def open_db(path: str, readonly: bool = True) -> sqlite3.Connection:
    """A short-lived extra connection (e.g. for a web request on a worker thread).
    Assumes ``connect`` already created the schema."""
    if readonly:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    else:
        db = sqlite3.connect(path, timeout=30)
        db.execute("PRAGMA synchronous=NORMAL")
    db.row_factory = sqlite3.Row
    return db


def pair_id(kalshi: str, pm: str) -> str:
    return f"{kalshi}|{pm}"
