"""State schema migration v5; keep DDL and index definitions stable."""

import sqlite3
import time

_V5_SCHEMA = {
    "order_drafts": """CREATE TABLE order_drafts (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200 AND length(trim(title)) > 0),
    proposed_amount_paise INTEGER NOT NULL CHECK(typeof(proposed_amount_paise)='integer' AND proposed_amount_paise BETWEEN 0 AND 1000000000000),
    currency TEXT NOT NULL CHECK(currency='INR'),
    status TEXT NOT NULL CHECK(status IN ('draft', 'cancelled')),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at)
)""",
}

def apply(conn: sqlite3.Connection) -> None:
    for statement in _V5_SCHEMA.values():
        conn.execute(statement)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (5, int(time.time())))
    conn.execute("PRAGMA user_version = 5")
