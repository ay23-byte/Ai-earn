"""State schema migration v21; keep DDL and index definitions stable."""

import sqlite3
import time

_V21_PROVIDER_ORDERS_ID_OWNER_IDX = "CREATE UNIQUE INDEX payment_provider_orders_id_owner_idx ON payment_provider_orders(id, owner_id)"

_V21_PROVIDER_ORDERS_PROVIDER_IDX = "CREATE UNIQUE INDEX payment_provider_orders_provider_order_idx ON payment_provider_orders(provider_order_id, owner_id)"

_V21_PROVIDER_EVENTS_ID_IDX = "CREATE UNIQUE INDEX payment_provider_events_id_owner_idx ON payment_provider_events(event_id, owner_id)"

_V21_SCHEMA = {
    "payment_provider_orders": """CREATE TABLE payment_provider_orders (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    provider TEXT NOT NULL CHECK(provider IN ('razorpay','stripe')),
    mode TEXT NOT NULL CHECK(mode IN ('sandbox','live')),
    amount_paise INTEGER NOT NULL CHECK(typeof(amount_paise)='integer' AND amount_paise BETWEEN 0 AND 1000000000000),
    currency TEXT NOT NULL CHECK(currency='INR'),
    status TEXT NOT NULL CHECK(status IN ('created','authorized','captured','settled','refunded','failed')),
    provider_order_id TEXT NOT NULL CHECK(length(provider_order_id) BETWEEN 1 AND 64 AND provider_order_id NOT GLOB '*[^A-Za-z0-9_-]*'),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at)
)""",
    "payment_provider_events": """CREATE TABLE payment_provider_events (
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    event_id TEXT PRIMARY KEY NOT NULL CHECK(length(event_id) BETWEEN 1 AND 64 AND event_id NOT GLOB '*[^A-Za-z0-9_-]*'),
    order_id TEXT NOT NULL CHECK(length(order_id)=32 AND order_id NOT GLOB '*[^0-9a-f]*'),
    event_type TEXT NOT NULL CHECK(event_type IN ('authorized','captured','failed','refunded','settled')),
    amount_paise INTEGER NOT NULL CHECK(typeof(amount_paise)='integer' AND amount_paise BETWEEN 0 AND 1000000000000),
    raw_body_hash BLOB NOT NULL CHECK(typeof(raw_body_hash)='blob' AND length(raw_body_hash)=32),
    received_at INTEGER NOT NULL CHECK(typeof(received_at)='integer' AND received_at >= 0),
    FOREIGN KEY(order_id, owner_id) REFERENCES payment_provider_orders(id, owner_id)
)""",
}

def apply(conn: sqlite3.Connection) -> None:
    for statement in _V21_SCHEMA.values():
        conn.execute(statement)
    conn.execute(_V21_PROVIDER_ORDERS_ID_OWNER_IDX)
    conn.execute(_V21_PROVIDER_ORDERS_PROVIDER_IDX)
    conn.execute(_V21_PROVIDER_EVENTS_ID_IDX)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (21, int(time.time())))
    conn.execute("PRAGMA user_version = 21")
