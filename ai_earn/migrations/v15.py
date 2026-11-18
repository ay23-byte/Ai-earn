"""State schema migration v15; keep DDL and index definitions stable."""

import sqlite3
import time

_V15_SCHEMA = {
    "opportunities": """CREATE TABLE opportunities (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200 AND length(trim(title)) > 0),
    source_url TEXT NOT NULL CHECK(length(source_url) BETWEEN 1 AND 2048 AND (source_url GLOB 'http://*' OR source_url GLOB 'https://*')),
    source_type TEXT NOT NULL CHECK(source_type IN ('manual','connector')),
    connector_id TEXT CHECK(connector_id IS NULL OR (length(connector_id) BETWEEN 1 AND 64 AND connector_id NOT GLOB '*[^A-Za-z0-9_-]*')),
    budget_paise INTEGER NOT NULL CHECK(typeof(budget_paise)='integer' AND budget_paise BETWEEN 0 AND 1000000000000),
    currency TEXT NOT NULL CHECK(currency='INR'),
    deadline_at INTEGER NOT NULL CHECK(typeof(deadline_at)='integer' AND deadline_at BETWEEN 0 AND 4102444800),
    risk_level TEXT NOT NULL CHECK(risk_level IN ('low','medium','high','unknown')),
    skill_tags TEXT NOT NULL CHECK(length(skill_tags) BETWEEN 0 AND 500),
    status TEXT NOT NULL CHECK(status IN ('discovered','vetted','pursuing','archived','dismissed')),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at)
)""",
}

def apply(conn: sqlite3.Connection) -> None:
    for statement in _V15_SCHEMA.values():
        conn.execute(statement)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (15, int(time.time())))
    conn.execute("PRAGMA user_version = 15")
