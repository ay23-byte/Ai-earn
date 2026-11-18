"""State schema migration v20; keep DDL and index definitions stable."""

import sqlite3
import time

_V20_INVOICE_INDEX = "CREATE UNIQUE INDEX invoices_id_owner_idx ON invoices(id, owner_id)"

_V20_INVOICE_NUMBER_INDEX = "CREATE UNIQUE INDEX invoices_number_owner_idx ON invoices(invoice_number, owner_id)"

_V20_SCHEMA = {
    "invoices": """CREATE TABLE invoices (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    order_id TEXT NOT NULL CHECK(length(order_id)=32 AND order_id NOT GLOB '*[^0-9a-f]*'),
    invoice_number TEXT NOT NULL CHECK(length(invoice_number) BETWEEN 1 AND 32 AND invoice_number NOT GLOB '*[^A-Za-z0-9_-]*'),
    amount_paise INTEGER NOT NULL CHECK(typeof(amount_paise)='integer' AND amount_paise BETWEEN 0 AND 1000000000000),
    currency TEXT NOT NULL CHECK(currency='INR'),
    status TEXT NOT NULL CHECK(status IN ('draft','issued','paid','overdue','void')),
    pdf_hash BLOB CHECK(pdf_hash IS NULL OR (typeof(pdf_hash)='blob' AND length(pdf_hash)=32)),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at),
    FOREIGN KEY(order_id, owner_id) REFERENCES order_drafts(id, owner_id)
)""",
}

def apply(conn: sqlite3.Connection) -> None:
    for statement in _V20_SCHEMA.values():
        conn.execute(statement)
    conn.execute(_V20_INVOICE_INDEX)
    conn.execute(_V20_INVOICE_NUMBER_INDEX)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (20, int(time.time())))
    conn.execute("PRAGMA user_version = 20")
