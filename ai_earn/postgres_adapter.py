"""Postgres Row-Level Security design PoC (P5-1) — not bundled, not enabled.

Design: one Postgres database, every owner-scoped table carries ``owner_id``,
and RLS policies restrict rows to ``current_setting('app.owner_id')`` set per
transaction by trusted server code. SQLite remains the shipped storage; this
module only renders the DDL/PoC contract and refuses to connect unless a
``psycopg`` driver is separately installed and a DSN is explicitly provided.
"""

from __future__ import annotations

import os

OWNER_SETTING = "app.owner_id"

RLS_POLICY_DDL = """\
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation ON {table}
  USING (owner_id = current_setting('app.owner_id'))
  WITH CHECK (owner_id = current_setting('app.owner_id'));
"""


def rls_ddl_for(tables: list[str]) -> str:
    """Render owner-isolation RLS DDL for the given owner-scoped tables."""
    if not isinstance(tables, list) or not tables:
        raise ValueError("At least one table is required.")
    for table in tables:
        if not isinstance(table, str) or not table.replace("_", "").isalnum() or not table[0].islower():
            raise ValueError("Invalid table name.")
    return "\n".join(RLS_POLICY_DDL.format(table=table) for table in tables)


def set_owner_sql(owner_id: str) -> tuple[str, tuple[str]]:
    """Parameterized statement binding the session owner for one transaction."""
    if not isinstance(owner_id, str) or len(owner_id) != 32 or any(c not in "0123456789abcdef" for c in owner_id):
        raise ValueError("Invalid owner.")
    return "SELECT set_config('app.owner_id', %s, true)", (owner_id,)


class PostgresAdapterUnavailable(RuntimeError):
    """Raised when the optional Postgres PoC cannot run in this environment."""


def connect(dsn: str | None = None):  # pragma: no cover - optional dependency
    """Connect only when psycopg is installed and a DSN is explicitly given."""
    dsn = dsn or os.environ.get("AI_EARN_POSTGRES_DSN", "")
    if not dsn:
        raise PostgresAdapterUnavailable("No Postgres DSN configured; SQLite remains the storage engine.")
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError as exc:
        raise PostgresAdapterUnavailable("psycopg is not installed; Postgres PoC is unavailable.") from exc
    return psycopg.connect(dsn)
