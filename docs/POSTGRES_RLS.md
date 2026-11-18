# Postgres Row-Level Security — design + PoC (P5-1)

**Status:** adapter implemented (`PostgresAdapter` with owner-bound transactions, `apply_rls`, `healthcheck`), tested with a fake driver. SQLite remains the default shipped storage; Postgres activates only with the optional `requirements-postgres.txt` dependency (`psycopg`) and an explicit `AI_EARN_POSTGRES_DSN`. No Postgres server is contacted by the test suite.

## Decision under evaluation

| Option | Summary | Verdict |
|---|---|---|
| Per-user SQLite vaults (current) | One private file per deployment, owner isolation enforced in application transactions and OS ACLs | **Shipped**; simplest, offline-first |
| Postgres + RLS | Shared database, `owner_id` column on every owner-scoped table, RLS policy `USING (owner_id = current_setting('app.owner_id'))` | PoC below; only if a hosted multi-user deployment is ever approved |

## PoC contract (`ai_earn/postgres_adapter.py`)

- `rls_ddl_for(tables)` renders `ENABLE ROW LEVEL SECURITY` + an
  `owner_isolation` policy per table (validated table names only).
- `set_owner_sql(owner_id)` returns a parameterized `set_config('app.owner_id', %s, true)`
  statement — the owner is bound per transaction by trusted server code,
  never interpolated.
- `connect()` refuses unless `psycopg` is separately installed **and**
  `AI_EARN_POSTGRES_DSN` is explicitly set; otherwise it raises
  `PostgresAdapterUnavailable` and SQLite remains authoritative.
- `PostgresAdapter.transaction(owner)` binds `app.owner_id` transaction-locally
  before yielding a cursor; `apply_rls(owner, tables)` enables RLS policies;
  `healthcheck(owner)` verifies the binding round-trips.

## Migration sketch (future, not implemented)

1. Create tables with the same columns as the SQLite v29 schema plus
   `owner_id` indexes; load `rls_ddl_for(...)` for every owner-scoped table.
2. Application opens a transaction, runs `set_owner_sql(owner)`, then queries
   without repeating owner predicates (RLS enforces them); integration tests
   must prove cross-owner reads return zero rows with the predicate omitted.
3. SQLite backup/restore remains the disaster-recovery path until parity is
   proven on a disposable Postgres instance.

## Acceptance for any future enablement

Disposable Postgres instance; RLS bypass attempts (missing setting, foreign
owner, superuser role excluded) all fail; full unittest suite green against
the adapter; separate human sign-off. Until then this is documentation only.

## Integration evidence (2026-11-18)

The gated test `tests/test_postgres_integration.py` was run against a real,
disposable local Postgres (via the `pgserver` sandbox package, not a repo
dependency): RLS policies applied through `PostgresAdapter.apply_rls`, and a
non-owner role bound to owner A saw only A's row while owner B saw only B's
row. The test skips unless `AI_EARN_POSTGRES_DSN` and
`AI_EARN_POSTGRES_APP_DSN` are set, so CI remains server-free.
