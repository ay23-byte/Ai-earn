# Database — Ai-earn (2026-11-17, branch arena/a2b30614-ai-earn)

**Status:** `EXPERIMENTAL` local SQLite only, **not production**. No Postgres, no network DB.

## Stores

- **Auth DB** (`AI_EARN_AUTH_DB`, `user_version 1`): `accounts(id, username UNIQUE, password_salt, password_hash, password_iterations, recovery_hash, created_at)`, `sessions(token_hash PK, account_id FK, expires_at, revoked_at)`, `auth_attempts(action, subject, occurred_at) + index`, `auth_audit(id PK, occurred_at, event, account_id FK)`. Private POSIX 0700/0600 or Windows ACL, `BEGIN IMMEDIATE`, `mode=rw` URI.

- **State DB** (`AI_EARN_STATE_DB`, `APPLICATION_ID 0x41494533`, `user_version 1→27`): `state_meta`, `state_migrations(version PK, applied_at)`, plus versioned tables:
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
  - `v20` `invoices` (order_id FK `(order_id, owner_id)` → `order_drafts`, invoice_number 1..32 `[A-Za-z0-9_-]`, amount 0..1e12 INR, status `draft→issued→paid/overdue/void`, pdf_hash BLOB 32 or NULL) + `invoices_id_owner_idx` + `invoices_number_owner_idx`
  - `v21` `payment_provider_orders` (provider razorpay/stripe, mode sandbox/live, amount 0..1e12 INR, status `created→authorized→captured→settled→refunded/failed`, provider_order_id 1..64) + `payment_provider_orders_id_owner_idx` + `payment_provider_orders_provider_order_idx` + `payment_provider_events` (event_id 1..64, order_id FK, event_type authorized/captured/settled/refunded/failed, raw_body_hash BLOB 32) + `payment_provider_events_id_owner_idx`
  - `v22` `ai_approvals` (tool_name 1..64, args_json 2..4096 bounded JSON, status `pending→approved→rejected→expired→revoked` with virtual expiry `pending+now>expires_at → expired`, created_at/expires_at TTL 60..86400, decided_at, `ai_approvals_id_owner_idx`) + `ai_emergency` (owner_id PK, stopped 0/1, updated_at, reason 0..500) + 1 index `ai_approvals_id_owner_idx`
  - `v23` `project_tasks` (opportunity FK CHECK-only, title 1-200, status `pending→in_progress→pending_review→completed/cancelled/retry_scheduled`, deadline_at, priority) + `project_task_dependencies` + `project_task_logs` + `project_task_timing` history
  - `v24` ALTER `project_tasks` lease (`lease_owner`, `lease_until`, `attempt_count`, `last_error`) migrate_to_v24
  - `v25` `service_packages`/`service_requirements`/`service_attachments`/`proposals`/`customer_requests` (service FK, customer FK, status state machines, ai_generated 0/1) + 5 indexes
  - `v26` `product_ideas` (title 1..200, description 0..2000, status `idea→validated→rejected→archived`) + `product_validations` (criteria 1..500, result pass/fail/pending, evidence 0..1000) FK products + `market_research`/`pricing_suggestions` (ai_generated 0/1, verification unverified/verified, evidence_url 0..2048 http) + `product_versions` (version 1..20 regex, notes 0..2000, updates products.version) + `product_files`/`product_deliverables` (file_name 1..200, file_hash BLOB 32, size 0..104857600, mime 1..100) + `product_sales` (quantity 1..1e6, amount 0..1e12, sale_at, evidence_url, verification) + `product_documentation` (title 1..200, content 1..10000) + 9 indexes
  - `v27` `customer_notes` (note 1..2000) + `customer_requirements` (requirement 1..500) + `customer_orders` (title 1..200, description 0..2000, amount 0..1e12 INR, status `draft→confirmed→fulfilled/cancelled`) + `customer_projects` (customer FK ↔ project_tasks FK, UNIQUE) + `crm_communication_approvals` (communication FK, status pending→approved/rejected/expired, TTL 60..86400, expires_at) + `crm_outbound_configs` (owner PK, enabled 0/1, allowed_channels 0..500, daily_limit 1..100) + 9 indexes

All tables have `CHECK (length(owner_id)=32 AND hex)` except `state_*`, plus `CHECK (owner_id = hex)`, and foreign keys to `owner_id`. Amounts are `CHECK typeof='integer' AND BETWEEN 0 AND 1e12`.

## Migrations

- Explicit, backup-first: `StateStorage.migrate_to_v2` … `migrate_to_v18, migrate_to_v19, migrate_to_v20, migrate_to_v21, migrate_to_v23, migrate_to_v24, migrate_to_v25, migrate_to_v26`, each holds `transaction` write lock, calls `backup_to(verified_new_path, confirmed=True)`, applies DDL, inserts `state_migrations`, `PRAGMA user_version = N`, then `validate_schema` + `check_integrity`. `backup_to` verifies source integrity, `create_exclusive` on destination (fails if exists), `source.backup(destination)`, verifies destination.
- New files start at `v1`; no auto-upgrade; unknown `user_version` or missing `state_migrations` history → `StateStorageError`.
- `tracker_cli._initialize_state` creates `state.sqlite3` (v1) then migrates `v2→v27` with 27 `before-v*.sqlite3` backups, never overwriting.

## Integrity & ownership

- `validate_schema` checks `application_id`, `user_version` in 1..28, expected tables/columns/indexes/sql, migration history `1..N`.
- `check_integrity` (`PRAGMA quick_check`, `foreign_key_check`, then versioned: `check_operation_results`, `check_audit_operation_balance`, `check_proposed_amount_snapshots`, `check_queue_integrity`, `check_creation_events`, `check_terminal_transition_events`, `check_multistep_histories` (now includes `opportunities` discovered→…→archived/dismissed, `services` draft→published→archived, `followups` pending→completed/cancelled, `invoices` draft→issued→paid/overdue/void), `check_paired_completions`, `check_managed_pairs`, `check_financial_assertion_binding`, `check_sandbox_payment_integrity`).
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
  - `invoices` (v20): `draft → issued → paid/overdue/void` (plus draft→void, issued→void, overdue→paid/void, PDF stub SHA-256)
  - `payment_provider_orders` (v21): `created → authorized → captured → settled → refunded/failed` (HMAC verified webhook, idempotent event_id, owner-scoped)
  - `ai_approvals` (v22): `pending→approved→rejected→expired(revoked)` (`pending` past TTL virtual expired, explicit confirmation, emergency stop, tool/args binding, bounded JSON, no auto-execution, `ai_approvals_id_owner_idx`)
  - `product_ideas` (v26): `idea→validated→rejected→archived` (FSM), `product_validations` pass/fail/pending (FK products), `market_research`/`pricing_suggestions` AI drafts `ai_generated=1` always `verification=unverified` unless evidence_url https + `verified` (fabrication guard), `product_versions` 1..20 regex updating `products.version`, `product_files`/`product_deliverables` file_hash32, `product_sales` quantity+amount split `total` vs `verified` (verified requires evidence_url), `product_documentation` title/content AI draft unverified
  - `customer_notes`/`customer_requirements` (v27): add/list/remove, `customer_orders` `draft→confirmed→fulfilled/cancelled`, `customer_projects` link→project_tasks history, `crm_communication_approvals` pending→approved/rejected/expired (TTL 15m, virtual expiry), `crm_outbound_configs` per-owner safe limited workflow (enabled+allowlist+daily_limit 1..100), outbound `send` requires approval unless safe workflow, no auto-send, timeline aggregated
  - `ai_emergency` (v22): per-owner stopped flag, `emergency_stop/clear` with confirmation, blocks consequential
  - `ledger_entries` (v18): append-only `ledger_entry_created`, net = income - expense - refund - fee (unverified), 11-concept `eleven_concepts` separates unverified proposed/quote/contract/invoice/initiated vs verified received/settled (synthetic) and net = settled(synthetic) - refunds - fees - costs
- `task_plans`: `planned → cancelled`
- `order_drafts`: `draft → cancelled`
- `invoice_drafts`: `draft → cancelled`
- `task_queue`: `queued ↔ leased → queued (retry) → exhausted | cancelled`, `queued → cancelled` (deadline)
- `task_workflows`: `planned → in_progress → pending_review → completed`, plus `pending_review → in_progress` (reject), `*_→cancelled`
- `payment_observations`: `unverified ↔ flagged → discarded`
- `financial_assertions`: `potential → quote_asserted → contract_asserted → invoice_asserted → initiated_asserted → withdrawn`
- `sandbox_payments`: `sandbox_awaiting → sandbox_confirmed → sandbox_settled → sandbox_refunded` plus `sandbox_failed` branch

Disposal: all data is disposable, validated via `python -m unittest discover -v` (963/17, incl. v15 opportunity discovery + managed runner + v16 services/portfolio + v17 products/customers + v18 comms/ledger + v19 followups/CSV + v20 invoices/11-concept + v21 provider sandbox HMAC + v22 AI ToolRegistry/ApprovalQueue, Linux Python 3.11.2). No customer or payment data, no production use.

See `ARCHITECTURE.md`, `SECURITY_REVIEW.md`, `TESTING.md` for limits, and `PHASE_STATUS.md` for phase gates. Real-data recovery and production hardening are future milestones (M2, M15).
