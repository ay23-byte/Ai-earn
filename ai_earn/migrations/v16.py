"""State schema migration v16; keep DDL and index definitions stable."""

import sqlite3
import time

_V16_INDEX = "CREATE UNIQUE INDEX services_id_owner_idx ON services(id, owner_id)"

_V16_SCHEMA = {
    "services": """CREATE TABLE services (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200 AND length(trim(title)) > 0),
    scope TEXT NOT NULL CHECK(length(scope) BETWEEN 0 AND 2000),
    deliverable TEXT NOT NULL CHECK(length(deliverable) BETWEEN 1 AND 500 AND length(trim(deliverable)) > 0),
    price_paise INTEGER NOT NULL CHECK(typeof(price_paise)='integer' AND price_paise BETWEEN 0 AND 1000000000000),
    currency TEXT NOT NULL CHECK(currency='INR'),
    revisions INTEGER NOT NULL CHECK(typeof(revisions)='integer' AND revisions BETWEEN 0 AND 100),
    status TEXT NOT NULL CHECK(status IN ('draft','published','archived')),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at)
)""",
    "portfolio_items": """CREATE TABLE portfolio_items (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    service_id TEXT NOT NULL CHECK(length(service_id)=32 AND service_id NOT GLOB '*[^0-9a-f]*'),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200 AND length(trim(title)) > 0),
    description TEXT NOT NULL CHECK(length(description) BETWEEN 0 AND 2000),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    FOREIGN KEY(service_id, owner_id) REFERENCES services(id, owner_id)
)""",
}

def apply(conn: sqlite3.Connection) -> None:
    for statement in _V16_SCHEMA.values():
        conn.execute(statement)
    conn.execute(_V16_INDEX)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (16, int(time.time())))
    conn.execute("PRAGMA user_version = 16")
