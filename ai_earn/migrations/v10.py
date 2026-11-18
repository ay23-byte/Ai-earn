"""State schema migration v10; keep DDL and index definitions stable."""

import sqlite3
import time

_V10_INDEX = "CREATE UNIQUE INDEX task_workflows_id_owner_idx ON task_workflows(id, owner_id)"

_V10_SCHEMA = {
    "task_associations": """CREATE TABLE task_associations (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    queue_id TEXT NOT NULL CHECK(length(queue_id)=32 AND queue_id NOT GLOB '*[^0-9a-f]*'),
    workflow_id TEXT NOT NULL CHECK(length(workflow_id)=32 AND workflow_id NOT GLOB '*[^0-9a-f]*'),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    UNIQUE(queue_id, owner_id),
    UNIQUE(workflow_id, owner_id),
    FOREIGN KEY(queue_id, owner_id) REFERENCES task_queue(id, owner_id),
    FOREIGN KEY(workflow_id, owner_id) REFERENCES task_workflows(id, owner_id)
)""",
}

def apply(conn: sqlite3.Connection) -> None:
    conn.execute(_V10_INDEX)
    for statement in _V10_SCHEMA.values():
        conn.execute(statement)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (10, int(time.time())))
    conn.execute("PRAGMA user_version = 10")
