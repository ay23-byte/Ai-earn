# Database Release Validation

## SQLite (shipped engine) — VERIFIED

- Schema v29; explicit checked-backup-first migrations; tamper checks and
  rollback tests in the suite; owner-bound queries on every sensitive
  resource (cross-owner denial tests across customers, projects, invoices,
  expenses, payments, proposals, opportunities, AI records, connectors).
- Tenant identity comes from the authenticated session, never client input.
- Concurrent-writer, crash-recovery, and backup/reopen drills pass
  (disposable data).

## PostgreSQL (optional adapter) — VERIFIED against a real server

- `PostgresAdapter`: per-transaction `set_config('app.owner_id', ..., true)`
  (transaction-local, so pooled connections cannot leak one owner's context
  into another's transaction), `FORCE ROW LEVEL SECURITY`, healthcheck.
- Real-server evidence (2026-11-18, disposable `pgserver` instance):
  gated test `tests/test_postgres_integration.py` PASSED — non-owner role
  bound to owner A saw only A's row; owner B only B's. Test skips unless
  `AI_EARN_POSTGRES_DSN` + `AI_EARN_POSTGRES_APP_DSN` are set; CI stays
  server-free. No connection pool is bundled: one connection per transaction
  is the safe default for this binding model; operators adding a pool must
  keep the transaction-local binding (never session-level `SET`).

## Remaining operator actions

- Hosted Postgres provisioning, TLS, backups, and role management are
  deployment decisions; SQLite remains the supported v1.0 default.
