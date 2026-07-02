"""SQLite storage. One file (default data/poe2.db), prices stored in Exalted
as the single source of truth. UNIQUE(item_id, ts) makes inserts idempotent."""

from __future__ import annotations

import os
import sqlite3

DEFAULT_DB = os.path.join("data", "poe2.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS leagues (
    value       TEXT PRIMARY KEY,
    short_name  TEXT,
    is_current  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS items (
    item_id     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    category    TEXT,
    api_id      TEXT,
    type        TEXT,
    icon_url    TEXT,
    current_price_exalted REAL
);

CREATE TABLE IF NOT EXISTS price_points (
    item_id       INTEGER NOT NULL REFERENCES items(item_id),
    ts            TEXT    NOT NULL,   -- ISO-8601 UTC, normalized (+00:00)
    price_exalted REAL    NOT NULL,
    quantity      INTEGER NOT NULL DEFAULT 0,
    UNIQUE(item_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_pp_item_ts ON price_points(item_id, ts);

-- per-item collector progress; makes backfill resumable:
-- backfilled=0 -> full history still needed, =1 -> only incremental updates
CREATE TABLE IF NOT EXISTS sync_state (
    item_id     INTEGER PRIMARY KEY REFERENCES items(item_id),
    backfilled  INTEGER NOT NULL DEFAULT 0,
    last_synced TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- user favorites: poe2scout items starred in the dashboard; the same item can
-- be favorited multiple times with different trade flags (e.g. unid vs id)
CREATE TABLE IF NOT EXISTS favorites (
    id              INTEGER PRIMARY KEY,
    item_id         INTEGER NOT NULL REFERENCES items(item_id),
    flag_identified TEXT NOT NULL DEFAULT 'any',
    flag_corrupted  TEXT NOT NULL DEFAULT 'any',
    flag_unrevealed TEXT NOT NULL DEFAULT 'any',
    trade_label     TEXT,
    added_ts        TEXT,
    tracking        INTEGER NOT NULL DEFAULT 1,  -- hourly trade snapshots on/off
    UNIQUE(item_id, flag_identified, flag_corrupted, flag_unrevealed)
);

-- watchlist: saved official-trade searches, polled by `trade collect`
CREATE TABLE IF NOT EXISTS trade_searches (
    search_id  TEXT PRIMARY KEY,   -- id from the trade URL, e.g. OgQmXJmOIE
    label      TEXT NOT NULL,
    league     TEXT NOT NULL,
    query_json TEXT NOT NULL,      -- cached query so polling needs no page fetch
    active     INTEGER NOT NULL DEFAULT 1,
    added_ts   TEXT
);

-- one price snapshot per watched search per poll (stats over cheapest listings)
CREATE TABLE IF NOT EXISTS trade_snapshots (
    search_id     TEXT NOT NULL REFERENCES trade_searches(search_id),
    ts            TEXT NOT NULL,
    total         INTEGER,          -- total matching listings
    n_used        INTEGER,          -- listings that went into the stats
    min_exalted   REAL,
    med10_exalted REAL,             -- median of the 10 cheapest
    prices_json   TEXT,             -- cheapest listings in exalted, for later use
    UNIQUE(search_id, ts)
);

-- output of `analyze`, one row per item per denomination
CREATE TABLE IF NOT EXISTS analysis_results (
    item_id          INTEGER NOT NULL REFERENCES items(item_id),
    denom            TEXT    NOT NULL,   -- 'divine' | 'exalted'
    entry_ts         TEXT,
    entry_price      REAL,
    peak_ts          TEXT,
    peak_price       REAL,
    current_price    REAL,
    roi_peak         REAL,
    roi_now          REAL,
    best_buy_ts      TEXT,
    best_buy_price   REAL,
    best_sell_ts     TEXT,
    best_sell_price  REAL,
    roi_buy_sell     REAL,
    max_drawdown     REAL,
    volatility       REAL,
    liquidity        REAL,
    illiquid         INTEGER NOT NULL DEFAULT 0,
    n_points         INTEGER,
    investment_score REAL,
    UNIQUE(item_id, denom)
);
"""


def connect(path: str = DEFAULT_DB) -> sqlite3.Connection:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """Migrations for DBs created by older versions (executescript only
    creates missing tables, it never alters existing ones)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(favorites)")}
    for col, ddl in [
        ("flag_identified", "TEXT NOT NULL DEFAULT 'any'"),
        ("flag_corrupted", "TEXT NOT NULL DEFAULT 'any'"),
        ("flag_unrevealed", "TEXT NOT NULL DEFAULT 'any'"),
        ("trade_label", "TEXT"),  # label of the auto-created trade search
        ("tracking", "INTEGER NOT NULL DEFAULT 1"),
    ]:
        if col not in cols:
            con.execute(f"ALTER TABLE favorites ADD COLUMN {col} {ddl}")

    # old layout had item_id as PRIMARY KEY -> one favorite per item; rebuild
    # so the same item can be favorited with several flag combinations
    pk_cols = [r[1] for r in con.execute("PRAGMA table_info(favorites)") if r[5]]
    if pk_cols == ["item_id"]:
        con.executescript("""
            ALTER TABLE favorites RENAME TO favorites_old;
            CREATE TABLE favorites (
                id              INTEGER PRIMARY KEY,
                item_id         INTEGER NOT NULL REFERENCES items(item_id),
                flag_identified TEXT NOT NULL DEFAULT 'any',
                flag_corrupted  TEXT NOT NULL DEFAULT 'any',
                flag_unrevealed TEXT NOT NULL DEFAULT 'any',
                trade_label     TEXT,
                added_ts        TEXT,
                tracking        INTEGER NOT NULL DEFAULT 1,
                UNIQUE(item_id, flag_identified, flag_corrupted, flag_unrevealed)
            );
            INSERT INTO favorites(item_id, flag_identified, flag_corrupted,
                                  flag_unrevealed, trade_label, added_ts, tracking)
                SELECT item_id, flag_identified, flag_corrupted,
                       flag_unrevealed, trade_label, added_ts, tracking
                FROM favorites_old;
            DROP TABLE favorites_old;
        """)
    con.commit()


def upsert_league(con, value: str, short_name: str, is_current: bool) -> None:
    con.execute(
        "INSERT INTO leagues(value, short_name, is_current) VALUES(?,?,?) "
        "ON CONFLICT(value) DO UPDATE SET short_name=excluded.short_name, "
        "is_current=excluded.is_current",
        (value, short_name, int(is_current)),
    )


def upsert_item(con, item: dict) -> None:
    con.execute(
        "INSERT INTO items(item_id, name, category, api_id, type, icon_url, "
        "current_price_exalted) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(item_id) DO UPDATE SET name=excluded.name, "
        "category=excluded.category, api_id=excluded.api_id, type=excluded.type, "
        "icon_url=excluded.icon_url, "
        "current_price_exalted=excluded.current_price_exalted",
        (
            item["item_id"], item["name"], item.get("category"),
            item.get("api_id"), item.get("type"), item.get("icon_url"),
            item.get("current_price_exalted"),
        ),
    )


def insert_points(con, item_id: int, points: list[dict]) -> int:
    """INSERT OR IGNORE price points; returns how many rows were actually new."""
    cur = con.executemany(
        "INSERT OR IGNORE INTO price_points(item_id, ts, price_exalted, quantity) "
        "VALUES(?,?,?,?)",
        [(item_id, p["ts"], p["price"], p["quantity"]) for p in points],
    )
    return max(cur.rowcount, 0)


def max_ts(con, item_id: int) -> str | None:
    row = con.execute(
        "SELECT MAX(ts) AS m FROM price_points WHERE item_id=?", (item_id,)
    ).fetchone()
    return row["m"]


def get_sync_state(con, item_id: int) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM sync_state WHERE item_id=?", (item_id,)
    ).fetchone()


def set_sync_state(con, item_id: int, backfilled: bool, last_synced: str) -> None:
    con.execute(
        "INSERT INTO sync_state(item_id, backfilled, last_synced) VALUES(?,?,?) "
        "ON CONFLICT(item_id) DO UPDATE SET backfilled=excluded.backfilled, "
        "last_synced=excluded.last_synced",
        (item_id, int(backfilled), last_synced),
    )


def set_meta(con, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO meta(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def checkpoint(con) -> None:
    """Fold the WAL file back into the main db file. Call before closing a
    connection whose db file is about to be committed to git - GitHub Actions
    checks out a fresh clone each run, so a separate -wal file never travels
    with it and would silently drop whatever hadn't been checkpointed yet."""
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def get_meta(con, key: str) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None
