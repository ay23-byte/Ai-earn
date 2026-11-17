"""State schema migration v22; keep DDL and index definitions stable."""

import sqlite3
import time

_V22_APPROVAL_INDEX = "CREATE UNIQUE INDEX ai_approvals_id_owner_idx ON ai_approvals(id, owner_id)"

_V22_SCHEMA = {
    "ai_approvals": """CREATE TABLE ai_approvals (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    tool_name TEXT NOT NULL CHECK(length(tool_name) BETWEEN 1 AND 64 AND tool_name NOT GLOB '*[^A-Za-z0-9_.-]*'),
    args_json TEXT NOT NULL CHECK(length(args_json) BETWEEN 2 AND 4096),
    status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','expired','revoked')),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    expires_at INTEGER NOT NULL CHECK(typeof(expires_at)='integer' AND expires_at BETWEEN created_at AND 4102444800),
    decided_at INTEGER CHECK(decided_at IS NULL OR (typeof(decided_at)='integer' AND decided_at >= created_at)),
    UNIQUE(id, owner_id)
)""" ,
    "ai_emergency": """CREATE TABLE ai_emergency (
    owner_id TEXT PRIMARY KEY NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    stopped INTEGER NOT NULL CHECK(stopped IN (0,1)),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= 0),
    reason TEXT NOT NULL CHECK(length(reason) BETWEEN 0 AND 500)
)""" ,
}

def apply(conn: sqlite3.Connection) -> None:
    for statement in _V22_SCHEMA.values():
        conn.execute(statement)
    conn.execute(_V22_APPROVAL_INDEX)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (22, int(time.time())))
    conn.execute("PRAGMA user_version = 22")
