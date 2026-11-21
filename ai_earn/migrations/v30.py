"""State schema migration v30: connect accepted proposals to one owner order."""

import sqlite3
import time

_V30_PROPOSAL_ORDER_INDEX = "CREATE UNIQUE INDEX proposal_orders_proposal_idx ON proposal_orders(proposal_id, owner_id)"

_V30_SCHEMA = {
    "proposal_orders": """CREATE TABLE proposal_orders (
    order_id TEXT PRIMARY KEY NOT NULL CHECK(length(order_id)=32 AND order_id NOT GLOB '*[^0-9a-f]*'),
    proposal_id TEXT NOT NULL CHECK(length(proposal_id)=32 AND proposal_id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    UNIQUE(order_id, owner_id),
    UNIQUE(proposal_id, owner_id),
    FOREIGN KEY(order_id, owner_id) REFERENCES order_drafts(id, owner_id),
    FOREIGN KEY(proposal_id, owner_id) REFERENCES proposals(id, owner_id)
)""",
}


def apply(conn: sqlite3.Connection) -> None:
    for statement in _V30_SCHEMA.values():
        conn.execute(statement)
    conn.execute(_V30_PROPOSAL_ORDER_INDEX)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (30, int(time.time())))
    conn.execute("PRAGMA user_version = 30")
