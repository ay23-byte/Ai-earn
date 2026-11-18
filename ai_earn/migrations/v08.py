"""State schema migration v8; keep DDL and index definitions stable."""

import sqlite3
import time

_V8_SCHEMA = {
    "task_workflows": """CREATE TABLE task_workflows (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200 AND length(trim(title)) > 0),
    status TEXT NOT NULL CHECK(status IN ('planned', 'in_progress', 'pending_review', 'completed', 'cancelled')),
    output_hash BLOB,
    output_size INTEGER,
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at),
    CHECK((status IN ('pending_review', 'completed') AND typeof(output_hash)='blob' AND length(output_hash)=32
           AND typeof(output_size)='integer' AND output_size BETWEEN 1 AND 4096) OR
          (status NOT IN ('pending_review', 'completed') AND output_hash IS NULL AND output_size IS NULL))
)""",
}

def apply(conn: sqlite3.Connection) -> None:
    for statement in _V8_SCHEMA.values():
        conn.execute(statement)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (8, int(time.time())))
    conn.execute("PRAGMA user_version = 8")
