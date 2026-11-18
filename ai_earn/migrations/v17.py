"""State schema migration v17; keep DDL and index definitions stable."""

import sqlite3
import time

_V17_INDEX = "CREATE UNIQUE INDEX products_id_owner_idx ON products(id, owner_id)"

_V17_SCHEMA = {
    "products": """CREATE TABLE products (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200 AND length(trim(title)) > 0),
    description TEXT NOT NULL CHECK(length(description) BETWEEN 0 AND 2000),
    version TEXT NOT NULL CHECK(length(version) BETWEEN 1 AND 20 AND version NOT GLOB '*[^A-Za-z0-9._-]*'),
    price_paise INTEGER NOT NULL CHECK(typeof(price_paise)='integer' AND price_paise BETWEEN 0 AND 1000000000000),
    currency TEXT NOT NULL CHECK(currency='INR'),
    status TEXT NOT NULL CHECK(status IN ('draft','published','archived')),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at)
)""",
    "customers": """CREATE TABLE customers (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 200 AND length(trim(name)) > 0),
    contact TEXT NOT NULL CHECK(length(contact) BETWEEN 1 AND 500),
    source TEXT NOT NULL CHECK(length(source) BETWEEN 0 AND 500),
    status TEXT NOT NULL CHECK(status IN ('lead','contacted','converted','rejected')),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at)
)""",
}

def apply(conn: sqlite3.Connection) -> None:
    for statement in _V17_SCHEMA.values():
        conn.execute(statement)
    conn.execute(_V17_INDEX)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (17, int(time.time())))
    conn.execute("PRAGMA user_version = 17")
