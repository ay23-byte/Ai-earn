"""State schema migration v7; keep DDL and index definitions stable."""

import sqlite3
import time

_V7_SCHEMA = {
    "task_queue": """CREATE TABLE task_queue (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200 AND length(trim(title)) > 0),
    status TEXT NOT NULL CHECK(status IN ('queued', 'leased', 'cancelled', 'exhausted')),
    priority INTEGER NOT NULL CHECK(typeof(priority)='integer' AND priority BETWEEN 0 AND 9),
    max_attempts INTEGER NOT NULL CHECK(typeof(max_attempts)='integer' AND max_attempts BETWEEN 1 AND 10),
    attempts INTEGER NOT NULL CHECK(typeof(attempts)='integer' AND attempts BETWEEN 0 AND max_attempts),
    backoff_seconds INTEGER NOT NULL CHECK(typeof(backoff_seconds)='integer' AND backoff_seconds BETWEEN 0 AND 86400),
    available_at INTEGER NOT NULL CHECK(typeof(available_at)='integer' AND available_at BETWEEN 0 AND 4102444800),
    deadline_at INTEGER NOT NULL CHECK(typeof(deadline_at)='integer' AND deadline_at BETWEEN 0 AND 4102444800),
    current_lease_id TEXT CHECK(current_lease_id IS NULL OR (length(current_lease_id)=32 AND current_lease_id NOT GLOB '*[^0-9a-f]*')),
    lease_until INTEGER CHECK(lease_until IS NULL OR (typeof(lease_until)='integer' AND lease_until BETWEEN 0 AND 4102444800)),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at BETWEEN 0 AND 4102444800),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at BETWEEN created_at AND 4102444800),
    CHECK((status='leased' AND current_lease_id IS NOT NULL AND lease_until IS NOT NULL) OR
          (status!='leased' AND current_lease_id IS NULL AND lease_until IS NULL)),
    UNIQUE(id, owner_id),
    FOREIGN KEY(current_lease_id, id, owner_id) REFERENCES queue_leases(id, queue_id, owner_id)
)""",
    "queue_leases": """CREATE TABLE queue_leases (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    queue_id TEXT NOT NULL CHECK(length(queue_id)=32 AND queue_id NOT GLOB '*[^0-9a-f]*'),
    attempt INTEGER NOT NULL CHECK(typeof(attempt)='integer' AND attempt BETWEEN 1 AND 10),
    lease_until INTEGER NOT NULL CHECK(typeof(lease_until)='integer' AND lease_until BETWEEN 0 AND 4102444800),
    FOREIGN KEY(queue_id, owner_id) REFERENCES task_queue(id, owner_id),
    UNIQUE(queue_id, attempt),
    UNIQUE(id, queue_id, owner_id)
)""",
}

def apply(conn: sqlite3.Connection) -> None:
    for statement in _V7_SCHEMA.values():
        conn.execute(statement)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (7, int(time.time())))
    conn.execute("PRAGMA user_version = 7")
