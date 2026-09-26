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

-- Small key/value state, e.g. which bankroll stored results were sized for.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Markets found by live discovery (discover.py) between full catalog refreshes.
CREATE TABLE IF NOT EXISTS discovered (
    venue TEXT NOT NULL,
    id TEXT NOT NULL,
    ts REAL NOT NULL,               -- when discovery added it
    listed_ts REAL,                 -- when the venue created it
    candidates INTEGER NOT NULL,    -- suggestions found for it
    PRIMARY KEY (venue, id)
);
CREATE INDEX IF NOT EXISTS discovered_ts ON discovered (ts);

-- Novig markets' two outcomes (novig.py): which one arbscan calls YES, and whether
-- takers pay only while the event is live (game markets) or always (futures).
CREATE TABLE IF NOT EXISTS novig_outcomes (
    market TEXT PRIMARY KEY,
    event TEXT,
    yes_outcome TEXT NOT NULL,
    no_outcome TEXT NOT NULL,
    fee_when_live INTEGER NOT NULL
);

-- Matcher suggestions pairing a Novig market with a Kalshi or Polymarket US one.
CREATE TABLE IF NOT EXISTS novig_candidates (
    venue TEXT NOT NULL,            -- the other venue: K | P
    other TEXT NOT NULL,            -- its market (Kalshi ticker | Polymarket US slug)
    novig TEXT NOT NULL,            -- Novig market id
    score REAL NOT NULL,
    relation TEXT NOT NULL,         -- 'same': Novig YES is the other market's YES; 'inverse': its NO
    confident INTEGER NOT NULL,
    created REAL NOT NULL,
    PRIMARY KEY (venue, other, novig)
);

-- Sampled gaps between Novig and another venue (novig_gaps.py): both books walked at
-- the bankroll, after fees, a few seconds apart.
CREATE TABLE IF NOT EXISTS novig_gaps (
    ts REAL NOT NULL,               -- when the Novig book was read
    done_ts REAL NOT NULL,          -- when the other venue's book was in too
    venue TEXT NOT NULL,            -- the other venue: K | P
    other TEXT NOT NULL,
    novig TEXT NOT NULL,
    direction TEXT NOT NULL,        -- N:YES+O:NO etc. (O = the other venue)
    top_edge REAL,                  -- $ per contract pair at the best prices, after fees
    size INTEGER,
    cost REAL,
    profit REAL,
    n_coef REAL,                    -- Novig's taker fee coefficient then (0 before a game starts)
    days REAL,
    pregame INTEGER,
    n_book TEXT,                    -- JSON top levels bought on Novig
    o_book TEXT                     -- and on the other venue
);
CREATE INDEX IF NOT EXISTS novig_gaps_ts ON novig_gaps (ts);

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
    days_to_resolve REAL,
    cut INTEGER                     -- 1: still open when the scanner stopped (resumed if it comes back soon)
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

-- Simulated trades (paper.py): both legs filled against the live books as they stood
-- when the orders would have reached each exchange. Nothing here was really traded.
CREATE TABLE IF NOT EXISTS paper_trades (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,               -- when the pick was seen and the orders "sent"
    pair TEXT NOT NULL,
    direction TEXT NOT NULL,
    k_side TEXT NOT NULL,           -- yes | no bought on Kalshi
    p_side TEXT NOT NULL,           -- yes | no bought on Polymarket US
    planned_size INTEGER,           -- contract pairs the book showed when deciding
    planned_edge REAL,
    planned_profit REAL,
    planned_cost REAL,
    k_limit REAL,                   -- limit prices sent (IOC)
    p_limit REAL,
    k_delay_ms REAL,                -- decision -> order at the exchange, as simulated
    p_delay_ms REAL,
    k_qty REAL,                     -- contracts bought (entry plus any chase)
    p_qty REAL,
    k_fees REAL,
    p_fees REAL,
    unwind_venue TEXT,              -- venue where an unhedged remainder was sold back
    unwind_qty REAL,
    unwind_loss REAL,
    k_hold REAL,                    -- contracts held to settlement
    p_hold REAL,
    k_out REAL,                     -- net cash spent on each venue (fees included)
    p_out REAL,
    locked_profit REAL,             -- min(k_hold, p_hold) - k_out - p_out: profit if both settle as one bet
    days REAL,
    resolve_ts REAL,                -- expected resolution
    status TEXT NOT NULL,           -- open | settled | missed
    settled_ts REAL,
    payout_k REAL,
    payout_p REAL,
    pnl REAL,
    note TEXT,
    books TEXT                      -- JSON: the ask ladders (top 5) seen when deciding and met on arrival, per leg
);
CREATE INDEX IF NOT EXISTS paper_ts ON paper_trades (ts);
CREATE INDEX IF NOT EXISTS paper_status ON paper_trades (status);

-- How each market we've paired settled (results.py): what one YES contract paid.
CREATE TABLE IF NOT EXISTS results (
    venue TEXT NOT NULL,            -- K | P
    id TEXT NOT NULL,               -- Kalshi ticker | Polymarket US slug
    status TEXT,                    -- the venue's status when last read
    yes_value REAL,                 -- $ paid per YES contract (0-1); NULL until the venue publishes a result
    result TEXT,                    -- Kalshi: yes | no | scalar; Polymarket US: the winning outcome's name
    closed_ts REAL,                 -- scheduled close / end
    settled_ts REAL,                -- when the venue determined or settled it (Kalshi only)
    first_final_ts REAL,            -- when we first saw the result
    checked_ts REAL NOT NULL,       -- last lookup
    raw TEXT,                       -- the venue's fields we read, JSON
    PRIMARY KEY (venue, id)
);

-- Every recorded window whose two markets have settled. payout_per_pair is what one
-- contract pair in the window's direction paid: 1.0 when both markets settled as one
-- bet (the arb worked), 0 or 2 when they didn't. peak_pairs * payout_per_pair -
-- cost_at_max is what taking the window at its peak would really have made.
CREATE VIEW IF NOT EXISTS window_outcomes AS
SELECT e.rowid AS episode, e.pair, e.direction, e.start_ts, e.end_ts, e.max_top_edge, e.max_profit, e.cost_at_max,
       e.max_profit + e.cost_at_max AS peak_pairs, e.days_to_resolve,
       rk.yes_value AS k_yes_value, rp.yes_value AS p_yes_value,
       (CASE WHEN e.direction LIKE 'K:YES%' THEN rk.yes_value ELSE 1 - rk.yes_value END)
         + (CASE WHEN e.direction LIKE '%P:YES' THEN rp.yes_value ELSE 1 - rp.yes_value END) AS payout_per_pair,
       (e.max_profit + e.cost_at_max)
         * ((CASE WHEN e.direction LIKE 'K:YES%' THEN rk.yes_value ELSE 1 - rk.yes_value END)
            + (CASE WHEN e.direction LIKE '%P:YES' THEN rp.yes_value ELSE 1 - rp.yes_value END))
         - e.cost_at_max AS realized_at_peak
FROM episodes e
JOIN results rk ON rk.venue = 'K' AND rk.id = substr(e.pair, 1, instr(e.pair, '|') - 1)
JOIN results rp ON rp.venue = 'P' AND rp.id = substr(e.pair, instr(e.pair, '|') + 1)
WHERE rk.yes_value IS NOT NULL AND rp.yes_value IS NOT NULL;

-- Approved pairs whose markets have both settled: did they settle as one bet?
CREATE VIEW IF NOT EXISTS pair_outcomes AS
SELECT d.kalshi, d.pm, d.decision AS relation, d.source, rk.yes_value AS k_yes_value, rp.yes_value AS p_yes_value,
       rk.result AS k_result, rp.result AS p_result,
       CASE WHEN d.decision = 'same' THEN abs(rk.yes_value - rp.yes_value) < 0.001
            ELSE abs(rk.yes_value + rp.yes_value - 1) < 0.001 END AS consistent,
       max(rk.first_final_ts, rp.first_final_ts) AS known_ts
FROM decisions d
JOIN results rk ON rk.venue = 'K' AND rk.id = d.kalshi
JOIN results rp ON rp.venue = 'P' AND rp.id = d.pm
WHERE d.decision IN ('same', 'inverse') AND rk.yes_value IS NOT NULL AND rp.yes_value IS NOT NULL;
"""


# Columns added after the first release, so older databases can be upgraded in place.
MIGRATIONS = {
    "decisions": [("source", "TEXT")],
    "episodes": [("cost_at_max", "REAL"), ("cut", "INTEGER")],
    "sweeps": [("best_edge", "REAL"), ("best_pair", "TEXT"), ("best_dir", "TEXT")],
    "paper_trades": [("books", "TEXT")],
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
