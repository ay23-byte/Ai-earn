"""Optional real-Postgres RLS integration test.

Skipped unless AI_EARN_POSTGRES_DSN and AI_EARN_POSTGRES_APP_DSN (a non-owner
role) are explicitly set. Run against a disposable database only; see
docs/POSTGRES_RLS.md. CI does not require a Postgres server.
"""

from __future__ import annotations

import os
import unittest

_DSN = os.environ.get("AI_EARN_POSTGRES_DSN", "")
_APP_DSN = os.environ.get("AI_EARN_POSTGRES_APP_DSN", "")


@unittest.skipUnless(_DSN and _APP_DSN, "requires disposable Postgres DSNs")
class PostgresRLSIntegrationTests(unittest.TestCase):
    def test_owner_isolation_with_real_postgres(self):
        psycopg = __import__("psycopg")
        from ai_earn.postgres_adapter import PostgresAdapter

        table = "ai_earn_rls_probe"
        admin = psycopg.connect(_DSN, autocommit=True)
        try:
            admin.execute(f"DROP TABLE IF EXISTS {table}")
            admin.execute(f"CREATE TABLE {table} (id text, owner_id text)")
            admin.execute(f"INSERT INTO {table} VALUES ('1', %s), ('2', %s)", ("a" * 32, "b" * 32))
            admin.execute(f"GRANT SELECT ON {table} TO PUBLIC")
        finally:
            admin.close()
        try:
            PostgresAdapter(dsn=_DSN).apply_rls("a" * 32, [table])
            app = PostgresAdapter(dsn=_APP_DSN)
            for owner, expected in (("a" * 32, ["1"]), ("b" * 32, ["2"])):
                with app.transaction(owner) as cur:
                    cur.execute(f"SELECT id FROM {table} ORDER BY id")
                    self.assertEqual([row[0] for row in cur.fetchall()], expected)
        finally:
            admin = psycopg.connect(_DSN, autocommit=True)
            try:
                admin.execute(f"DROP TABLE IF EXISTS {table}")
            finally:
                admin.close()


if __name__ == "__main__":
    unittest.main()
