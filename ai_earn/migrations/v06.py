"""State schema migration v6; keep DDL and index definitions stable."""

import sqlite3
import time

_V6_INDEX = "CREATE UNIQUE INDEX order_drafts_id_owner_idx ON order_drafts(id, owner_id)"

_V6_SCHEMA = {
    "invoice_drafts": """CREATE TABLE invoice_drafts (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    order_id TEXT NOT NULL CHECK(length(order_id)=32 AND order_id NOT GLOB '*[^0-9a-f]*'),
    proposed_amount_paise INTEGER NOT NULL CHECK(typeof(proposed_amount_paise)='integer' AND proposed_amount_paise BETWEEN 0 AND 1000000000000),
    currency TEXT NOT NULL CHECK(currency='INR'),
    status TEXT NOT NULL CHECK(status IN ('draft', 'cancelled')),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at),
    FOREIGN KEY(order_id, owner_id) REFERENCES order_drafts(id, owner_id)
)""",
}

def apply(conn: sqlite3.Connection) -> None:
    conn.execute(_V6_INDEX)
    for statement in _V6_SCHEMA.values():
        conn.execute(statement)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (6, int(time.time())))
    conn.execute("PRAGMA user_version = 6")
