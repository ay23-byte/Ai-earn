"""State schema migration v23; keep DDL and index definitions stable."""

import sqlite3
import time

_V23_PROJECT_TASKS_INDEX = "CREATE UNIQUE INDEX project_tasks_id_owner_idx ON project_tasks(id, owner_id)"

_V23_DEPENDS_INDEX = "CREATE INDEX task_dependencies_task_idx ON task_dependencies(task_id, owner_id)"

_V23_LOGS_INDEX = "CREATE INDEX task_execution_logs_task_idx ON task_execution_logs(task_id, owner_id)"

_V23_SCHEMA = {
    "project_tasks": """CREATE TABLE project_tasks (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    opportunity_id TEXT CHECK(opportunity_id IS NULL OR (length(opportunity_id)=32 AND opportunity_id NOT GLOB '*[^0-9a-f]*')),
    parent_task_id TEXT CHECK(parent_task_id IS NULL OR (length(parent_task_id)=32 AND parent_task_id NOT GLOB '*[^0-9a-f]*')),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200 AND length(trim(title)) > 0),
    description TEXT NOT NULL CHECK(length(description) BETWEEN 0 AND 2000),
    priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 9),
    deadline_at INTEGER NOT NULL CHECK(deadline_at BETWEEN 0 AND 4102444800),
    estimated_duration_seconds INTEGER NOT NULL CHECK(estimated_duration_seconds BETWEEN 0 AND 31536000),
    actual_duration_seconds INTEGER NOT NULL CHECK(actual_duration_seconds BETWEEN 0 AND 31536000),
    status TEXT NOT NULL CHECK(status IN ('draft','pending_review','approved','scheduled','in_progress','completed','failed','cancelled','rejected')),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(updated_at >= created_at)
)""",
    "task_dependencies": """CREATE TABLE task_dependencies (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    task_id TEXT NOT NULL CHECK(length(task_id)=32 AND task_id NOT GLOB '*[^0-9a-f]*'),
    depends_on_task_id TEXT NOT NULL CHECK(length(depends_on_task_id)=32 AND depends_on_task_id NOT GLOB '*[^0-9a-f]*'),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    UNIQUE(task_id, depends_on_task_id, owner_id),
    CHECK(task_id != depends_on_task_id)
)""",
    "task_execution_logs": """CREATE TABLE task_execution_logs (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    task_id TEXT NOT NULL CHECK(length(task_id)=32 AND task_id NOT GLOB '*[^0-9a-f]*'),
    message TEXT NOT NULL CHECK(length(message) BETWEEN 1 AND 2000),
    created_at INTEGER NOT NULL CHECK(created_at >= 0)
)""",
}

def apply(conn: sqlite3.Connection) -> None:
    for statement in _V23_SCHEMA.values():
        conn.execute(statement)
    conn.execute(_V23_PROJECT_TASKS_INDEX)
    conn.execute(_V23_DEPENDS_INDEX)
    conn.execute(_V23_LOGS_INDEX)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (23, int(time.time())))
    conn.execute("PRAGMA user_version = 23")
