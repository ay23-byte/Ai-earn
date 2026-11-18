"""State schema migration v4; keep DDL and index definitions stable."""

import sqlite3
import time

_V4_SCHEMA = {
    "task_plans": """CREATE TABLE task_plans (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200 AND length(trim(title)) > 0),
    status TEXT NOT NULL CHECK(status IN ('planned', 'cancelled')),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at)
)""",
}

def apply(conn: sqlite3.Connection) -> None:
    for statement in _V4_SCHEMA.values():
        conn.execute(statement)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (4, int(time.time())))
    conn.execute("PRAGMA user_version = 4")
