"""State schema migration v14; keep DDL and index definitions stable."""

import sqlite3
import time

_V14_SCHEMA = {
    "sandbox_payments": """CREATE TABLE sandbox_payments (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    amount_paise INTEGER NOT NULL CHECK(typeof(amount_paise)='integer' AND amount_paise BETWEEN 0 AND 1000000000000),
    currency TEXT NOT NULL CHECK(currency='INR'),
    key_fingerprint BLOB NOT NULL CHECK(typeof(key_fingerprint)='blob' AND length(key_fingerprint)=32),
    stage TEXT NOT NULL CHECK(stage IN ('sandbox_awaiting', 'sandbox_confirmed', 'sandbox_settled', 'sandbox_failed', 'sandbox_refunded')),
    sequence INTEGER NOT NULL CHECK(typeof(sequence)='integer' AND sequence BETWEEN 0 AND 3),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at),
    UNIQUE(id, owner_id)
)""",
    "sandbox_payment_events": """CREATE TABLE sandbox_payment_events (
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    event_id TEXT NOT NULL CHECK(length(event_id) BETWEEN 1 AND 64 AND event_id NOT GLOB '*[^A-Za-z0-9_-]*'),
    payment_id TEXT NOT NULL CHECK(length(payment_id)=32 AND payment_id NOT GLOB '*[^0-9a-f]*'),
    event_type TEXT NOT NULL CHECK(event_type IN ('confirmation', 'settlement', 'failure', 'refund')),
    sequence INTEGER NOT NULL CHECK(typeof(sequence)='integer' AND sequence BETWEEN 1 AND 3),
    body_hash BLOB NOT NULL CHECK(typeof(body_hash)='blob' AND length(body_hash)=32),
    PRIMARY KEY(owner_id, event_id),
    UNIQUE(payment_id, sequence),
    FOREIGN KEY(payment_id, owner_id) REFERENCES sandbox_payments(id, owner_id)
)""",
}

def apply(conn: sqlite3.Connection) -> None:
    for statement in _V14_SCHEMA.values():
        conn.execute(statement)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (14, int(time.time())))
    conn.execute("PRAGMA user_version = 14")
