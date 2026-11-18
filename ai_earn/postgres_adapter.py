"""Postgres Row-Level Security adapter (P5-1) — optional, off by default.

One Postgres database, every owner-scoped table carries ``owner_id``, and
RLS policies restrict rows to ``current_setting('app.owner_id')`` set per
transaction by trusted server code. SQLite remains the default shipped
storage; Postgres activates only when ``psycopg`` is installed (see
``requirements-postgres.txt``) and ``AI_EARN_POSTGRES_DSN`` is explicitly
set. The adapter never falls back silently and never stores credentials.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

OWNER_SETTING = "app.owner_id"

RLS_POLICY_DDL = """\
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
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




class PostgresAdapter:
    """Owner-scoped Postgres adapter enforcing RLS session binding.

    Wraps a psycopg connection factory. Every transaction first binds
    ``app.owner_id`` (transaction-local), so RLS — not application code —
    is the final isolation boundary.
    """

    def __init__(self, *, dsn: str | None = None, connection_factory=None):
        self._dsn = dsn or os.environ.get("AI_EARN_POSTGRES_DSN", "")
        self._factory = connection_factory

    def _connect(self):
        if self._factory is not None:
            return self._factory(self._dsn)
        return connect(self._dsn)

    @contextmanager
    def transaction(self, owner_id: str):
        """Yield a cursor inside a transaction bound to ``owner_id``."""
        sql, params = set_owner_sql(owner_id)
        conn = self._connect()
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def apply_rls(self, owner_id: str, tables: list[str]) -> None:
        """Enable RLS + owner-isolation policy on each table (idempotent-ish).

        Intended for migrations run by a privileged role once; policies use
        DROP IF EXISTS so re-runs are safe.
        """
        ddl = rls_ddl_for(tables)
        with self.transaction(owner_id) as cur:
            for table in tables:
                cur.execute(f"DROP POLICY IF EXISTS owner_isolation ON {table}")
            for statement in ddl.split(";"):
                if statement.strip():
                    cur.execute(statement)

    def healthcheck(self, owner_id: str) -> bool:
        """Return True when a transaction can bind the owner and read the setting."""
        with self.transaction(owner_id) as cur:
            cur.execute("SELECT current_setting('app.owner_id')")
            row = cur.fetchone()
            return bool(row) and row[0] == owner_id
