"""State schema migration v25; keep DDL and index definitions stable."""

import sqlite3
import time

_V25_PACKAGE_INDEX = "CREATE UNIQUE INDEX service_packages_id_owner_idx ON service_packages(id, owner_id)"

_V25_REQUIREMENT_INDEX = "CREATE INDEX service_requirements_service_idx ON service_requirements(service_id, owner_id)"

_V25_ATTACHMENT_INDEX = "CREATE INDEX service_attachments_service_idx ON service_attachments(service_id, owner_id)"

_V25_PROPOSAL_INDEX = "CREATE UNIQUE INDEX proposals_id_owner_idx ON proposals(id, owner_id)"

_V25_REQUEST_INDEX = "CREATE UNIQUE INDEX customer_requests_id_owner_idx ON customer_requests(id, owner_id)"

_V25_SCHEMA = {
    "service_packages": """CREATE TABLE service_packages (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    service_id TEXT NOT NULL CHECK(length(service_id)=32 AND service_id NOT GLOB '*[^0-9a-f]*'),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200 AND length(trim(title)) > 0),
    description TEXT NOT NULL CHECK(length(description) BETWEEN 0 AND 2000),
    price_paise INTEGER NOT NULL CHECK(typeof(price_paise)='integer' AND price_paise BETWEEN 0 AND 1000000000000),
    delivery_days INTEGER NOT NULL CHECK(typeof(delivery_days)='integer' AND delivery_days BETWEEN 1 AND 365),
    revisions INTEGER NOT NULL CHECK(typeof(revisions)='integer' AND revisions BETWEEN 0 AND 100),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at),
    FOREIGN KEY(service_id, owner_id) REFERENCES services(id, owner_id)
)""",
    "service_requirements": """CREATE TABLE service_requirements (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    service_id TEXT NOT NULL CHECK(length(service_id)=32 AND service_id NOT GLOB '*[^0-9a-f]*'),
    requirement TEXT NOT NULL CHECK(length(requirement) BETWEEN 1 AND 500 AND length(trim(requirement)) > 0),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    FOREIGN KEY(service_id, owner_id) REFERENCES services(id, owner_id)
)""",
    "service_attachments": """CREATE TABLE service_attachments (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    service_id TEXT CHECK(service_id IS NULL OR (length(service_id)=32 AND service_id NOT GLOB '*[^0-9a-f]*')),
    portfolio_item_id TEXT CHECK(portfolio_item_id IS NULL OR (length(portfolio_item_id)=32 AND portfolio_item_id NOT GLOB '*[^0-9a-f]*')),
    file_name TEXT NOT NULL CHECK(length(file_name) BETWEEN 1 AND 200 AND length(trim(file_name)) > 0),
    file_hash BLOB CHECK(file_hash IS NULL OR (typeof(file_hash)='blob' AND length(file_hash)=32)),
    file_size INTEGER NOT NULL CHECK(typeof(file_size)='integer' AND file_size BETWEEN 0 AND 104857600),
    mime_type TEXT NOT NULL CHECK(length(mime_type) BETWEEN 1 AND 100),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    CHECK((service_id IS NOT NULL) OR (portfolio_item_id IS NOT NULL))
)""",
    "proposals": """CREATE TABLE proposals (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    service_id TEXT NOT NULL CHECK(length(service_id)=32 AND service_id NOT GLOB '*[^0-9a-f]*'),
    customer_id TEXT CHECK(customer_id IS NULL OR (length(customer_id)=32 AND customer_id NOT GLOB '*[^0-9a-f]*')),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200 AND length(trim(title)) > 0),
    description TEXT NOT NULL CHECK(length(description) BETWEEN 0 AND 2000),
    price_paise INTEGER NOT NULL CHECK(typeof(price_paise)='integer' AND price_paise BETWEEN 0 AND 1000000000000),
    delivery_days INTEGER NOT NULL CHECK(typeof(delivery_days)='integer' AND delivery_days BETWEEN 1 AND 365),
    status TEXT NOT NULL CHECK(status IN ('draft','sent','accepted','rejected','cancelled','archived')),
    ai_generated INTEGER NOT NULL CHECK(ai_generated IN (0,1)),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at),
    FOREIGN KEY(service_id, owner_id) REFERENCES services(id, owner_id)
)""",
    "customer_requests": """CREATE TABLE customer_requests (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    service_id TEXT NOT NULL CHECK(length(service_id)=32 AND service_id NOT GLOB '*[^0-9a-f]*'),
    customer_id TEXT NOT NULL CHECK(length(customer_id)=32 AND customer_id NOT GLOB '*[^0-9a-f]*'),
    message TEXT NOT NULL CHECK(length(message) BETWEEN 1 AND 2000),
    status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected','completed','cancelled')),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at),
    FOREIGN KEY(service_id, owner_id) REFERENCES services(id, owner_id),
    FOREIGN KEY(customer_id, owner_id) REFERENCES customers(id, owner_id)
)""",
}

def apply(conn: sqlite3.Connection) -> None:
    for statement in _V25_SCHEMA.values():
        conn.execute(statement)
    conn.execute(_V25_PACKAGE_INDEX)
    conn.execute(_V25_REQUIREMENT_INDEX)
    conn.execute(_V25_ATTACHMENT_INDEX)
    conn.execute(_V25_PROPOSAL_INDEX)
    conn.execute(_V25_REQUEST_INDEX)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (25, int(time.time())))
    conn.execute("PRAGMA user_version = 25")
