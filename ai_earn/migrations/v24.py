"""State schema migration v24; keep DDL and index definitions stable."""

import sqlite3
import time

_V24_LEASE_COL = "lease_id TEXT CHECK(lease_id IS NULL OR (length(lease_id)=32 AND lease_id NOT GLOB '*[^0-9a-f]*'))"

_V24_UNTIL_COL = "lease_until INTEGER CHECK(lease_until IS NULL OR (lease_until BETWEEN 0 AND 4102444800))"

_V24_ATTEMPTS_COL = "attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 10)"

_V24_MAX_ATTEMPTS_COL = "max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts BETWEEN 1 AND 10)"

_V24_BACKOFF_COL = "backoff_seconds INTEGER NOT NULL DEFAULT 60 CHECK(backoff_seconds BETWEEN 0 AND 86400)"

_V24_AVAILABLE_COL = "available_at INTEGER NOT NULL DEFAULT 0 CHECK(available_at BETWEEN 0 AND 4102444800)"

_V24_SCHEMA = {
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
    updated_at INTEGER NOT NULL CHECK(updated_at >= created_at),
    lease_id TEXT CHECK(lease_id IS NULL OR (length(lease_id)=32 AND lease_id NOT GLOB '*[^0-9a-f]*')),
    lease_until INTEGER CHECK(lease_until IS NULL OR (lease_until BETWEEN 0 AND 4102444800)),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 10),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts BETWEEN 1 AND 10),
    backoff_seconds INTEGER NOT NULL DEFAULT 60 CHECK(backoff_seconds BETWEEN 0 AND 86400),
    available_at INTEGER NOT NULL DEFAULT 0 CHECK(available_at BETWEEN 0 AND 4102444800)
)""",
}

def apply(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE project_tasks ADD COLUMN lease_id TEXT CHECK(lease_id IS NULL OR (length(lease_id)=32 AND lease_id NOT GLOB '*[^0-9a-f]*'))")
    conn.execute("ALTER TABLE project_tasks ADD COLUMN lease_until INTEGER CHECK(lease_until IS NULL OR (lease_until BETWEEN 0 AND 4102444800))")
    conn.execute("ALTER TABLE project_tasks ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 10)")
    conn.execute("ALTER TABLE project_tasks ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts BETWEEN 1 AND 10)")
    conn.execute("ALTER TABLE project_tasks ADD COLUMN backoff_seconds INTEGER NOT NULL DEFAULT 60 CHECK(backoff_seconds BETWEEN 0 AND 86400)")
    conn.execute("ALTER TABLE project_tasks ADD COLUMN available_at INTEGER NOT NULL DEFAULT 0 CHECK(available_at BETWEEN 0 AND 4102444800)")
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (24, int(time.time())))
    conn.execute("PRAGMA user_version = 24")
