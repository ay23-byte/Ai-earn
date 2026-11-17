"""State schema migration v2; keep DDL and index definitions stable."""

import sqlite3
import time

_V2_SCHEMA = {
    "state_audit": """CREATE TABLE state_audit (
    id INTEGER PRIMARY KEY,
    occurred_at INTEGER NOT NULL CHECK(typeof(occurred_at)='integer' AND occurred_at >= 0),
    actor_id TEXT NOT NULL CHECK(length(actor_id) BETWEEN 1 AND 128),
    event TEXT NOT NULL CHECK(length(event) BETWEEN 1 AND 64),
    resource_type TEXT NOT NULL CHECK(length(resource_type) BETWEEN 1 AND 64),
    resource_id TEXT NOT NULL CHECK(length(resource_id) BETWEEN 1 AND 128)
)""",
    "state_operations": """CREATE TABLE state_operations (
    owner_id TEXT NOT NULL CHECK(length(owner_id) BETWEEN 1 AND 128),
    operation_key TEXT NOT NULL CHECK(length(operation_key) BETWEEN 1 AND 128),
    request_hash BLOB NOT NULL CHECK(typeof(request_hash)='blob' AND length(request_hash)=32),
    resource_type TEXT NOT NULL CHECK(length(resource_type) BETWEEN 1 AND 64),
    resource_id TEXT NOT NULL CHECK(length(resource_id) BETWEEN 1 AND 128),
    PRIMARY KEY(owner_id, operation_key)
)""",
}

def apply(conn: sqlite3.Connection) -> None:
    # Infrastructure only; these tables do not provide authenticated actions,
    # complete business audit, or idempotency semantics by themselves.
    for statement in _V2_SCHEMA.values():
        conn.execute(statement)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (2, int(time.time())))
    conn.execute("PRAGMA user_version = 2")
