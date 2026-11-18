"""State schema migration v9; keep DDL and index definitions stable."""

import sqlite3
import time

_V9_INDEX = "CREATE UNIQUE INDEX invoice_drafts_id_owner_idx ON invoice_drafts(id, owner_id)"

_V9_SCHEMA = {
    "payment_observations": """CREATE TABLE payment_observations (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    invoice_id TEXT NOT NULL CHECK(length(invoice_id)=32 AND invoice_id NOT GLOB '*[^0-9a-f]*'),
    proposed_amount_paise INTEGER NOT NULL CHECK(typeof(proposed_amount_paise)='integer' AND proposed_amount_paise BETWEEN 0 AND 1000000000000),
    currency TEXT NOT NULL CHECK(currency='INR'),
    status TEXT NOT NULL CHECK(status IN ('unverified', 'flagged', 'discarded')),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at),
    FOREIGN KEY(invoice_id, owner_id) REFERENCES invoice_drafts(id, owner_id)
)""",
}

def apply(conn: sqlite3.Connection) -> None:
    conn.execute(_V9_INDEX)
    for statement in _V9_SCHEMA.values():
        conn.execute(statement)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (9, int(time.time())))
    conn.execute("PRAGMA user_version = 9")
