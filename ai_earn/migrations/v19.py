"""State schema migration v19; keep DDL and index definitions stable."""

import sqlite3
import time

_V19_CUSTOMER_INDEX = "CREATE UNIQUE INDEX customers_id_owner_idx ON customers(id, owner_id)"

_V19_INDEX = "CREATE UNIQUE INDEX followups_id_owner_idx ON followups(id, owner_id)"

_V19_SCHEMA = {
    "followups": """CREATE TABLE followups (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    customer_id TEXT NOT NULL CHECK(length(customer_id)=32 AND customer_id NOT GLOB '*[^0-9a-f]*'),
    due_at INTEGER NOT NULL CHECK(typeof(due_at)='integer' AND due_at BETWEEN 0 AND 4102444800),
    note TEXT NOT NULL CHECK(length(note) BETWEEN 0 AND 500),
    status TEXT NOT NULL CHECK(status IN ('pending','completed','cancelled')),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at),
    FOREIGN KEY(customer_id, owner_id) REFERENCES customers(id, owner_id)
)""",
}

def apply(conn: sqlite3.Connection) -> None:
    conn.execute(_V19_CUSTOMER_INDEX)
    for statement in _V19_SCHEMA.values():
        conn.execute(statement)
    conn.execute(_V19_INDEX)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (19, int(time.time())))
    conn.execute("PRAGMA user_version = 19")
