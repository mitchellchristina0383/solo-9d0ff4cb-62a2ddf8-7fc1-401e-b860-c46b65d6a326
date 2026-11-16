"""SQLite database setup and schema management."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .config import DB_PATH, DATA_DIR


def get_db_path() -> Path:
    return Path(os.environ.get("WATER_DB_PATH", DB_PATH))


def get_connection(db_path: str | os.PathLike | None = None):
    path = Path(db_path) if db_path is not None else get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS upload_batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_type      TEXT NOT NULL CHECK (batch_type IN ('reading','replacement')),
    filename        TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('committed','rejected')),
    row_count       INTEGER NOT NULL DEFAULT 0,
    reject_reason   TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES upload_batches(id),
    account     TEXT NOT NULL,
    meter       TEXT NOT NULL,
    read_at     TEXT NOT NULL,            -- ISO-8601 UTC, e.g. 2026-01-02T08:30:00Z
    reading_cum REAL NOT NULL CHECK (reading_cum >= 0),
    created_at  TEXT NOT NULL,
    UNIQUE (account, meter, read_at)
);

CREATE TABLE IF NOT EXISTS replacements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        INTEGER NOT NULL REFERENCES upload_batches(id),
    account         TEXT NOT NULL,
    old_meter       TEXT NOT NULL,
    new_meter       TEXT NOT NULL,
    switched_at     TEXT NOT NULL,       -- ISO-8601 UTC
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    status              TEXT NOT NULL CHECK (status IN ('active','expired','failed')),
    status_detail       TEXT,
    reading_batch_ids   TEXT NOT NULL,    -- comma separated committed reading batch ids at generation time
    replacement_batch_id INTEGER,          -- latest committed replacement batch id at generation time
    created_at          TEXT NOT NULL,
    superseded_at       TEXT
);

CREATE TABLE IF NOT EXISTS result_rows (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id       INTEGER NOT NULL REFERENCES generations(id),
    account             TEXT NOT NULL,
    meter               TEXT NOT NULL,
    interval_start      TEXT,
    interval_end        TEXT,
    start_reading       REAL,
    end_reading         REAL,
    consumption_m3      REAL,
    attributed_day      TEXT,             -- UTC YYYY-MM-DD of the later reading
    status              TEXT NOT NULL,    -- OK or REVIEW
    review_reason       TEXT,
    source_reading_ids  TEXT NOT NULL     -- comma separated readings.id
);

CREATE INDEX IF NOT EXISTS idx_readings_lookup ON readings(account, meter, read_at);
CREATE INDEX IF NOT EXISTS idx_replacements_acct ON replacements(account);
CREATE INDEX IF NOT EXISTS idx_results_gen ON result_rows(generation_id);
"""


def init_db(db_path: str | os.PathLike | None = None) -> Path:
    path = Path(db_path) if db_path is not None else get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return path
