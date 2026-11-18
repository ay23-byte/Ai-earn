"""State schema migration v12; keep DDL and index definitions stable."""

import sqlite3
import time

_V12_SCHEMA = {
    "managed_task_pairs": """CREATE TABLE managed_task_pairs (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    FOREIGN KEY(id, owner_id) REFERENCES task_associations(id, owner_id)
)""",
}

def apply(conn: sqlite3.Connection) -> None:
    for statement in _V12_SCHEMA.values():
        conn.execute(statement)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (12, int(time.time())))
    conn.execute("PRAGMA user_version = 12")
