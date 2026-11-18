"""State schema migration v27; keep DDL and index definitions stable."""

import sqlite3
import time

_V27_NOTE_INDEX = "CREATE INDEX customer_notes_customer_idx ON customer_notes(customer_id, owner_id)"

_V27_REQUIREMENT_INDEX = "CREATE INDEX customer_requirements_customer_idx ON customer_requirements(customer_id, owner_id)"

_V27_ORDER_INDEX = "CREATE UNIQUE INDEX customer_orders_id_owner_idx ON customer_orders(id, owner_id)"

_V27_ORDER_CUSTOMER_INDEX = "CREATE INDEX customer_orders_customer_idx ON customer_orders(customer_id, owner_id)"

_V27_PROJECT_CUSTOMER_INDEX = "CREATE INDEX customer_projects_customer_idx ON customer_projects(customer_id, owner_id)"

_V27_PROJECT_TASK_INDEX = "CREATE INDEX customer_projects_task_idx ON customer_projects(project_task_id, owner_id)"

_V27_COMM_APPROVAL_INDEX = "CREATE UNIQUE INDEX crm_communication_approvals_id_owner_idx ON crm_communication_approvals(id, owner_id)"

_V27_COMM_APPROVAL_COMM_INDEX = "CREATE INDEX crm_communication_approvals_comm_idx ON crm_communication_approvals(communication_id, owner_id)"

_V27_OUTBOUND_INDEX = "CREATE UNIQUE INDEX crm_outbound_configs_owner_idx ON crm_outbound_configs(owner_id)"

_V27_SCHEMA = {
    "customer_notes": """CREATE TABLE customer_notes (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    customer_id TEXT NOT NULL CHECK(length(customer_id)=32 AND customer_id NOT GLOB '*[^0-9a-f]*'),
    note TEXT NOT NULL CHECK(length(note) BETWEEN 1 AND 2000 AND length(trim(note)) > 0),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    FOREIGN KEY(customer_id, owner_id) REFERENCES customers(id, owner_id)
)""",
    "customer_requirements": """CREATE TABLE customer_requirements (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    customer_id TEXT NOT NULL CHECK(length(customer_id)=32 AND customer_id NOT GLOB '*[^0-9a-f]*'),
    requirement TEXT NOT NULL CHECK(length(requirement) BETWEEN 1 AND 500 AND length(trim(requirement)) > 0),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    FOREIGN KEY(customer_id, owner_id) REFERENCES customers(id, owner_id)
)""",
    "customer_orders": """CREATE TABLE customer_orders (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    customer_id TEXT NOT NULL CHECK(length(customer_id)=32 AND customer_id NOT GLOB '*[^0-9a-f]*'),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200 AND length(trim(title)) > 0),
    description TEXT NOT NULL CHECK(length(description) BETWEEN 0 AND 2000),
    amount_paise INTEGER NOT NULL CHECK(typeof(amount_paise)='integer' AND amount_paise BETWEEN 0 AND 1000000000000),
    currency TEXT NOT NULL CHECK(currency='INR'),
    status TEXT NOT NULL CHECK(status IN ('draft','confirmed','fulfilled','cancelled')),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at),
    FOREIGN KEY(customer_id, owner_id) REFERENCES customers(id, owner_id)
)""",
    "customer_projects": """CREATE TABLE customer_projects (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    customer_id TEXT NOT NULL CHECK(length(customer_id)=32 AND customer_id NOT GLOB '*[^0-9a-f]*'),
    project_task_id TEXT NOT NULL CHECK(length(project_task_id)=32 AND project_task_id NOT GLOB '*[^0-9a-f]*'),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    UNIQUE(customer_id, project_task_id, owner_id),
    FOREIGN KEY(customer_id, owner_id) REFERENCES customers(id, owner_id),
    FOREIGN KEY(project_task_id, owner_id) REFERENCES project_tasks(id, owner_id)
)""",
    "crm_communication_approvals": """CREATE TABLE crm_communication_approvals (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    communication_id TEXT NOT NULL CHECK(length(communication_id)=32 AND communication_id NOT GLOB '*[^0-9a-f]*'),
    status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','expired')),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    expires_at INTEGER NOT NULL CHECK(typeof(expires_at)='integer' AND expires_at > created_at AND (expires_at - created_at) BETWEEN 60 AND 86400),
    decided_at INTEGER CHECK(decided_at IS NULL OR typeof(decided_at)='integer'),
    FOREIGN KEY(communication_id, owner_id) REFERENCES communications(id, owner_id)
)""",
    "crm_outbound_configs": """CREATE TABLE crm_outbound_configs (
    owner_id TEXT PRIMARY KEY NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    allowed_channels TEXT NOT NULL CHECK(length(allowed_channels) BETWEEN 0 AND 500),
    daily_limit INTEGER NOT NULL CHECK(typeof(daily_limit)='integer' AND daily_limit BETWEEN 1 AND 100),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= 0)
)""",
}

def apply(conn: sqlite3.Connection) -> None:
    for statement in _V27_SCHEMA.values():
        conn.execute(statement)
    conn.execute(_V27_NOTE_INDEX)
    conn.execute(_V27_REQUIREMENT_INDEX)
    conn.execute(_V27_ORDER_INDEX)
    conn.execute(_V27_ORDER_CUSTOMER_INDEX)
    conn.execute(_V27_PROJECT_CUSTOMER_INDEX)
    conn.execute(_V27_PROJECT_TASK_INDEX)
    conn.execute(_V27_COMM_APPROVAL_INDEX)
    conn.execute(_V27_COMM_APPROVAL_COMM_INDEX)
    conn.execute(_V27_OUTBOUND_INDEX)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (27, int(time.time())))
    conn.execute("PRAGMA user_version = 27")
