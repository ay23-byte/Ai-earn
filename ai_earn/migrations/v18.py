"""State schema migration v18; keep DDL and index definitions stable."""

import sqlite3
import time

_V18_INDEX = "CREATE UNIQUE INDEX communications_id_owner_idx ON communications(id, owner_id)"

_V18_LEDGER_INDEX = "CREATE UNIQUE INDEX ledger_entries_id_owner_idx ON ledger_entries(id, owner_id)"

_V18_SCHEMA = {
    "communications": """CREATE TABLE communications (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    customer_id TEXT NOT NULL CHECK(length(customer_id)=32 AND customer_id NOT GLOB '*[^0-9a-f]*'),
    channel TEXT NOT NULL CHECK(length(channel) BETWEEN 1 AND 30),
    body TEXT NOT NULL CHECK(length(body) BETWEEN 1 AND 2000),
    status TEXT NOT NULL CHECK(status IN ('draft','sent','archived')),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at)
)""",
    "ledger_entries": """CREATE TABLE ledger_entries (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    kind TEXT NOT NULL CHECK(kind IN ('income','expense','refund','fee')),
    amount_paise INTEGER NOT NULL CHECK(typeof(amount_paise)='integer' AND amount_paise BETWEEN 0 AND 1000000000000),
    currency TEXT NOT NULL CHECK(currency='INR'),
    note TEXT NOT NULL CHECK(length(note) BETWEEN 0 AND 500),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0)
)""",
}

def apply(conn: sqlite3.Connection) -> None:
    for statement in _V18_SCHEMA.values():
        conn.execute(statement)
    conn.execute(_V18_INDEX)
    conn.execute(_V18_LEDGER_INDEX)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (18, int(time.time())))
    conn.execute("PRAGMA user_version = 18")
