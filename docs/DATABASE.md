# Database — Ai-earn (2026-11-16, branch arena/c6693e06-ai-earn)

**Status:** `EXPERIMENTAL` local SQLite only, **not production**. No Postgres, no network DB.

## Stores

- **Auth DB** (`AI_EARN_AUTH_DB`, `user_version 1`): `accounts(id, username UNIQUE, password_salt, password_hash, password_iterations, recovery_hash, created_at)`, `sessions(token_hash PK, account_id FK, expires_at, revoked_at)`, `auth_attempts(action, subject, occurred_at) + index`, `auth_audit(id PK, occurred_at, event, account_id FK)`. Private POSIX 0700/0600 or Windows ACL, `BEGIN IMMEDIATE`, `mode=rw` URI.

- **State DB** (`AI_EARN_STATE_DB`, `APPLICATION_ID 0x41494533`, `user_version 1→19`): `state_meta`, `state_migrations(version PK, applied_at)`, plus versioned tables:
  - `v2` `state_audit`, `state_operations(owner_id, operation_key) PK, request_hash, resource_type, resource_id`
  - `v3` `opportunity_drafts`
  - `v4` `task_plans`
  - `v5` `order_drafts` (INR paise 0..1e12)
  - `v6` `invoice_drafts` FK `(order_id, owner_id)` + `order_drafts_id_owner_idx`
  - `v7` `task_queue` + `queue_leases` (priority, max_attempts, backoff, available_at, deadline_at, lease_until)
  - `v8` `task_workflows` (output_hash BLOB, output_size)
  - `v9` `payment_observations` FK `(invoice_id, owner_id)` + `invoice_drafts_id_owner_idx`
  - `v10` `task_associations` + `task_workflows_id_owner_idx`
  - `v11` `paired_completions` + `task_associations_id_owner_idx`
  - `v12` `managed_task_pairs`
  - `v13` `financial_assertions` (unverified stages)
  - `v14` `sandbox_payments` (key_fingerprint, stage, sequence) + `sandbox_payment_events(owner_id, event_id PK, payment_id, event_type, sequence, body_hash)`
  - `v15` `opportunities` (title, source_url, source_type, connector_id, budget_paise, deadline_at, risk_level, skill_tags, status `discovered→vetted→pursuing→archived/dismissed`)
  - `v16` `services` (title, scope, deliverable, price_paise, revisions, status `draft→published→archived`) + `portfolio_items` (service FK, title, description) + `services_id_owner_idx`
  - `v17` `products` (title 1-200, description 0-2000, version 1-20 `[A-Za-z0-9._-]` , price_paise 0..1e12 INR, status `draft→published→archived`) + `customers` (name 1-200, contact 1-500, source 0-500, status `lead→contacted→converted/rejected`) + `products_id_owner_idx`
  - `v18` `communications` (customer_id FK, channel 1-30, body 1-2000, status `draft→sent→archived`) + `ledger_entries` (kind income/expense/refund/fee, amount 0..1e12 INR, note 0-500) + `communications_id_owner_idx` + `ledger_entries_id_owner_idx`
  - `v19` `followups` (customer_id FK `(customer_id, owner_id)`, due_at 0..4102444800, note 0..500, status `pending→completed/cancelled`) + `customers_id_owner_idx` + `followups_id_owner_idx`

All tables have `CHECK (length(owner_id)=32 AND hex)` except `state_*`, plus `CHECK (owner_id = hex)`, and foreign keys to `owner_id`. Amounts are `CHECK typeof='integer' AND BETWEEN 0 AND 1e12`.

## Migrations

- Explicit, backup-first: `StateStorage.migrate_to_v2` … `migrate_to_v18, migrate_to_v19`, each holds `transaction` write lock, calls `backup_to(verified_new_path, confirmed=True)`, applies DDL, inserts `state_migrations`, `PRAGMA user_version = N`, then `validate_schema` + `check_integrity`. `backup_to` verifies source integrity, `create_exclusive` on destination (fails if exists), `source.backup(destination)`, verifies destination.
- New files start at `v1`; no auto-upgrade; unknown `user_version` or missing `state_migrations` history → `StateStorageError`.
- `tracker_cli._initialize_state` creates `state.sqlite3` (v1) then migrates `v2→v19` with 18 `before-v*.sqlite3` backups, never overwriting.

## Integrity & ownership

- `validate_schema` checks `application_id`, `user_version` in 1..19, expected tables/columns/indexes/sql, migration history `1..N`.
- `check_integrity` (`PRAGMA quick_check`, `foreign_key_check`, then versioned: `check_operation_results`, `check_audit_operation_balance`, `check_proposed_amount_snapshots`, `check_queue_integrity`, `check_creation_events`, `check_terminal_transition_events`, `check_multistep_histories` (now includes `opportunities` discovered→…→archived/dismissed, `services` draft→published→archived, `followups` pending→completed/cancelled), `check_paired_completions`, `check_managed_pairs`, `check_financial_assertion_binding`, `check_sandbox_payment_integrity`).
- All domain services use `state_authorization._authorized_transaction` (auth lock → state lock, derive actor from live session, recheck expiry before commit). Queries always `WHERE owner_id=?`.
- `state_operations` idempotency is owner-scoped; coordinated deletion of marker+operation+audit can evade checks (not tamper-evident). Auth/state commits are separate → ambiguous on crash after state commit; caller must retry with same key and inspect `get_lease`/`inspect`.

## Recovery

- `StateStorage.backup_to` / restore = `backup_to` to new path (verified). Failed copy leaves partial file for inspection.
- `StateRecoveryComparison.compare_records` / `compare_snapshot` (recognised rows only, aggregate counts, auth-first deterministic locking, `quick_check` + `foreign_key_check` before compare). Not a restore, not freshness proof.
- `doctor.py` read-only `mode=ro&immutable=1`, parent privacy before file, `integrity_check`, no secret read.

## State machines (summary)

- `opportunity_drafts`: `draft → archived`
- `opportunities` (v15): `discovered → vetted → pursuing → archived/dismissed`, plus `discovered→archived/dismissed`, `vetted→archived/dismissed`
- `services` (v16): `draft → published → archived`, plus `draft→archived`
  - `products` (v17): `draft → published → archived`, plus `draft→archived`
  - `customers` (v17): `lead → contacted → converted`, `lead → rejected`, `contacted → rejected`
  - `communications` (v18): `draft → sent → archived`, plus `draft→archived`
  - `followups` (v19): `pending → completed/cancelled` (plus `pending` creation)
  - `ledger_entries` (v18): append-only `ledger_entry_created`, net = income - expense - refund - fee (unverified)
- `task_plans`: `planned → cancelled`
- `order_drafts`: `draft → cancelled`
- `invoice_drafts`: `draft → cancelled`
- `task_queue`: `queued ↔ leased → queued (retry) → exhausted | cancelled`, `queued → cancelled` (deadline)
- `task_workflows`: `planned → in_progress → pending_review → completed`, plus `pending_review → in_progress` (reject), `*_→cancelled`
- `payment_observations`: `unverified ↔ flagged → discarded`
- `financial_assertions`: `potential → quote_asserted → contract_asserted → invoice_asserted → initiated_asserted → withdrawn`
- `sandbox_payments`: `sandbox_awaiting → sandbox_confirmed → sandbox_settled → sandbox_refunded` plus `sandbox_failed` branch

Disposal: all data is disposable, validated via `python -m unittest discover -v` (732/17, incl. v15 opportunity discovery + managed runner + v16 services/portfolio + v17 products/customers + v18 comms/ledger + v19 followups/CSV, Linux Python 3.11.2). No customer or payment data, no production use.

See `ARCHITECTURE.md`, `SECURITY_REVIEW.md`, `TESTING.md` for limits, and `PHASE_STATUS.md` for phase gates. Real-data recovery and production hardening are future milestones (M2, M15).
