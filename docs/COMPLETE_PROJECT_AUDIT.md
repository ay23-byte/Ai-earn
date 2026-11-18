# Complete Project Audit — Ai-earn

**Date:** 2026-11-17  
**Branch:** `arena/4bd4230e-ai-earn` (active, branched from `6cd8b36` `main` post PR #41)  
**Commit:** `6cd8b36 Merge pull request #41` (offline-ready `production_ready:true` when healthy, `live_blocked:true`)  
**Auditors:** Senior Architect / Principal Python / Security / QA / DB Architect / Product (evidence-based, no code modified)  
**Scope:** Full repository `https://github.com/ay23-byte/Ai-earn` — local directory `/home/user/Ai-earn` (Linux `Python 3.11.2`, stdlib-only, no `requirements.txt`)  
**Method:** Traced execution from CLI input → auth → state storage → business logic → audit/backup → dashboard snapshot; inspected every `ai_earn/*.py` (62 files, ~23.8k LOC), `dashboard/*`, `docs/*`, `tests/*` (1014 tests), `tools/*`, `.github/workflows/tests.yml`, `README.md`; ran `status/health/doctor --json`, `compileall`, `security_check`, `unittest discover`; reproduced disposable-DB lifecycle (`YES` vs `yes`, username regex, zero-account auth).

---

## 1. Executive Summary

**Verdict:** Ai-earn is a **well-engineered offline prototype** (experimental, local, disposable-data) — **not production-ready** for real money, real customers, or multi-user deployment. It achieves its **approved smaller-product scope** (offline manual tracker + sandbox payment + AI draft + analytics + hardening) with **evidence-backed experimental quality**, but intentionally leaves live payments, verified income, automation, and web-serving unimplemented and blocked.

| Dimension | Status | Evidence |
|---|---|---|
| **Secure local accounts** | EXPERIMENTAL / PASS_WITH_LIMITATIONS | `auth.py` PBKDF2 600k, 30-min sessions, `_USERNAME=[a-z0-9_]{3,32}`, rate limits, `auth_storage.py` 0600 + Windows ACL, 17 tests Linux + 17 skipped Windows; user-reported Windows gate `72241cc` passed. Not a network security boundary. |
| **Storage & recovery** | EXPERIMENTAL / PASS_WITH_LIMITATIONS | `state_storage.py` v1→v28 (LATEST 28, `APPLICATION_ID 0x41494533`, WAL, `supported 1..28`), `migrate_to_vN` creates `before-vN.sqlite3` (27 files) holding write lock, `backup_to` verified, `doctor.py` read-only classify. Linux 963+ tests pass; real-data recovery unverified; stale backup revives old credentials. |
| **Business dashboard** | PARTIAL (real but limited) | `dashboard_metrics.py` + `dashboard_cli.py` + `dashboard/connected.html` produce **real owner-scoped aggregates** (counts/amounts from owned rows, 1 MB cap, no token/title), not mocked. No HTTP API, manual `snapshot` → static `http.server --bind 127.0.0.1`. |
| **Earning workspace** | EXPERIMENTAL (functional but draft-only) | `opportunities.py` v15 manual/connector, `services.py`/`portfolio` v16, `products`/`customers` v17, `communications`/`ledger` v18, `followups`/CSV v19, `invoices` v20, `payment_providers` v21 sandbox HMAC, `tracker_cli.py` 1-45 menu. All states are local unverified drafts; no external settlement. |
| **Financial management** | EXPERIMENTAL (unverified ledger) | `ledger.py` 4-kind (`income/expense/refund/fee`) + `invoices.py` 5-state + `financial_assertions.py` + 11-concept (`payment_observations` vs `ledger_entries` vs `durable_sandbox_payments`), `order_drafts`/`invoice_drafts` pipeline. Amounts are owner-asserted, `MRR` is unverified sum. No reconciliation with bank. |
| **AI assistant** | EXPERIMENTAL (stub only) | `ai_assistant.py` (1.1k LOC) `ToolRegistry` (32 tools A/B/C), `ApprovalQueue` with TTL, `ProviderConfig` (OpenAI/hosted/Ollama/stub, stub has no network). Requires explicit human approval for consequential actions; injection isolation and emergency stop implemented. No real LLM call unless configured. |
| **Automation / integrations** | MOCKED / STUB | `ai_earn/connectors/*` (`GmailConnector`, `DriveConnector`, `GitHubConnector`, `MarketplaceConnector`) — interfaces + `client.py`/`retry.py`/`throttle.py`/`auth.py` but **no live network verification**; `FakeConnector` for tests with revocation. |
| **Production readiness** | **NOT READY** (by design) | `status` reports offline capability; `health` reports actual DB `ok`; `production_readiness` 11-item gate + `production_config` hardening; live blocked (`ProviderConfig mode=live raises`, `live_blocked:true`). Offline ready when healthy, but missing prod secrets, Postgres RLS, TLS, WAF, monitoring, legal. |

**What works today (verified on Linux):**
- `python -m ai_earn status` / `health --json` / `doctor --json` / `tracker init/open` / `dashboard snapshot` with disposable private DBs
- Compilation `python -m compileall -q`, secret scan `tools/security_check` + `--audit`, `git diff --check`
- Full test suite **1014 tests, 17 skipped (Windows-only), 0 failures, 271s** (previously observed 963+17, now 1014+17 after PR40/41)
- 27 migration backups, owner-isolated CRUD with idempotency `operation_key` + `state_operations`/`state_audit` replay

**What is unsafe / blocked:**
- Live `payment_providers` HMAC is **sandbox-only** (no network, no bank), `finance.py`/`payment_sandbox.py` never moves money
- No HTTP API, no web auth, no CSRF/cookies, no multi-user RLS — SQLite single-user vault only
- Stale `backup_to` restores old sessions/passwords; Windows ACL not verified in this sandbox

---

## 2. Current Architecture

**Layers (modular, no circular imports observed via `compileall`):**
```
CLI (__main__.py → auth_cli / tracker_cli / dashboard_cli / doctor_cli / health_cli / data_cli)
  → Config (config.py: Settings, DatabasePaths, load_settings) + Events (events.py allowlist) + Responses (responses.py envelope)
  → Auth (auth.py AuthService + auth_storage.py AuthStorage PBKDF2) → Storage gates (windows_security.py, POSIX 0600)
  → State (state_storage.py StateStorage v1..v28 + state_authorization._authorized_transaction + state_audit)
  → Business domains:
     opportunities / opportunity_drafts / task_queue / task_workflows / task_associations / task_plans / task_runner / project_tasks / project_task_runner
     services / portfolio / products / customers / communications / ledger / followups / invoices / invoice_drafts / order_drafts / financial_assertions / financial_draft_workflows / payment_observations / payment_providers / payment_sandbox / durable_sandbox_payments
     service_workspace (packages/requirements/attachments/proposals/requests/templates/portfolio presentation + ServiceEditor)
     product_workspace (ideas/validations/research/pricing/versions/files/sales/deliverables/docs + ProductEditor)
     crm (notes/requirements/orders/projects + CRMCommunications / ActivityTimeline / CustomerProfile / CustomerCSV)
     analytics / revenue_analytics / dashboard_metrics
     ai_assistant (ToolRegistry, ApprovalQueue, 4 providers)
     connectors/* (base/client/retry/throttle/auth)
  → Cross-cutting: backup.py, secrets.py, structured_logging.py, error_reporting.py, monitoring.py, production_config.py, production_readiness.py, data_export.py, account_deletion.py
  → Front-end: dashboard/app.html|app.js|connected.html|snapshot.js (vanilla SPA 17 views, 1 MB cap) + static snapshot JSON (no API)
```

**Entry points:** `ai_earn/__main__.py:main()` dispatches `status`, `health`, `doctor`, `tracker`, `auth`, `dashboard`, `data`. `tracker_cli.run()` (1.9k LOC) is the primary UX (45-item menu 1-45).

**Config centralization:** `config.py` validates `AI_EARN_ENV ∈ {development,test,production}`, `AI_EARN_LOG_LEVEL`, `AI_EARN_AUTH_DB`, `AI_EARN_STATE_DB` (absolute, same private parent, not under `dashboard/www`). Errors never echo values (`ConfigurationError.variable` only). No `.env` auto-load.

**Error handling:** Consistent `AuthError`/`AccessDenied`/`RateLimitError`, `StateStorageError`, domain-specific `*Error` (e.g., `OpportunityError`, `ServiceError`), `error_reporting.SafeError` (8-hex `error_id`, redacted logs). All health/doctor paths are non-throwing (`check` returns `ok:false` with `checks`).

**Abstractions:** `Connector` ABC justified for marketplace/Gmail/Drive/GitHub stubs; `StateStorage` transaction context managers; `AuthService` clock-injectable (`Callable[[], float]`) for determinism.

**Duplicated/dead code:** Minor: `_paise` parsing duplicated in `tracker_cli.py` and `test_tracker_cli.py`; `dashboard_metrics.py` vs `revenue_analytics.py` overlapping ledger calculations (intentional separation: dashboard vs analytics). No obvious dead modules; `finance.py` (legacy) coexists with `ledger.py` (hardened) — historical, not removed.

---

## 3. Repository Structure

```
Ai-earn/
├── ai_earn/ (62 py, ~23.8k LOC)
│   ├── core: __init__.py, __main__.py, config.py, events.py, responses.py, status.py, health.py, health_cli.py, doctor.py, doctor_cli.py
│   ├── auth: auth.py, auth_cli.py, auth_storage.py, windows_security.py
│   ├── state: state_storage.py (2752 LOC), state_audit.py, state_authorization.py, state_recovery_comparison.py, backup.py, structured_logging.py, error_reporting.py, secrets.py, monitoring.py, production_config.py, production_readiness.py
│   ├── finance: finance.py, ledger.py, invoices.py, invoice_drafts.py, order_drafts.py, financial_assertions.py, financial_draft_workflows.py, payment_observations.py, payment_providers.py, payment_sandbox.py, durable_sandbox_payments.py, analytics.py, revenue_analytics.py
│   ├── crm: customers.py, communications.py, followups.py, crm.py, products.py
│   ├── opportunities: opportunities.py, opportunity_drafts.py
│   ├── tasks: task_queue.py, task_runner.py, task_workflows.py, task_associations.py, task_plans.py, project_tasks.py, project_task_runner.py, task_plans
│   ├── services: services.py, service_workspace.py, portfolio
│   ├── products: product_workspace.py
│   ├── ai: ai_assistant.py (1151 LOC)
│   ├── connectors/: base.py, client.py, retry.py, throttle.py, auth.py, gmail.py, drive.py, github.py, marketplace.py, registry.py, etc.
│   ├── dashboard: dashboard_cli.py, dashboard_metrics.py
│   ├── data: data_export.py, data_cli.py, account_deletion.py, tracker_cli.py (1974 LOC)
│   └── windows_security.py
├── dashboard/ (6 files): app.html, app.js, app.css, connected.html, index.html, snapshot.js — vanilla, 17 views
├── docs/ (25 md, ~3.8k LOC): ARCHITECTURE.md, PHASE_STATUS.md (585 LOC), TESTING.md, SECURITY.md, PRODUCTION_READINESS.md, DEPLOYMENT.md, etc.
├── tests/ (60 py): test_*.py covering 15 categories, 1014 tests
├── tools/ (3 py): security_check.py, windows_terminal_smoke.py, windows_candidate_smoke.py
├── .github/workflows/tests.yml (121 LOC, ubuntu+windows × 3.11/3.13, compileall, secret grep, pip-audit placeholder, unittest, health/doctor)
├── .env.example, .gitignore, README.md (14.5k)
└── no pyproject.toml / requirements.txt (stdlib-only intentional)
```

**Counts:** `wc -l ai_earn/*.py ai_earn/connectors/*.py` → 23,873 total; `docs/*.md` 3,794; `tests/test_*.py` ~60 suites.

**Git:** `branch arena/4bd4230e-ai-earn` active from `6cd8b36` (main after PR41); working tree clean; no untracked files; remote `origin` synced.

---

## 4. Functional Implementation Matrix

| ID | Feature | Impl Path | Tests (evidence) | Verdict | Notes |
|---|---|---|---|---|---|
| **CORE** | Install/status | `config.py`, `status.py`, `__main__.py` | `test_config` `test_cli` `test_events` (15) + `compileall` | **COMPLETE** (offline) | No live deploy |
| **SEC-001** | Registration/login/sessions | `auth.py:AuthService`, `auth_storage.py`, `auth_cli.py` | `test_auth` `test_auth_cli` `test_auth_terminal` + Windows 17 skipped | **PARTIAL** | PBKDF2 600k, `_USERNAME` lowercase, 30-min, rate-limit; not web |
| **SEC-002** | Authorization/audit | `state_authorization._authorized_transaction`, `state_audit` | `test_state_authorization` `test_state_audit` + cross-owner denial in every domain | **PARTIAL** | Owner-scoped per-table, not global |
| **DATA-001** | 40+ tables state machines | `state_storage` v1-v28 + domain modules | `test_state_*` `test_opportunities` `test_services` `test_products` `test_customers` `test_communications` `test_ledger` `test_followups` `test_invoices` `test_payment_providers` `test_service_workspace` `test_product_workspace` `test_crm` | **EXPERIMENTAL (COMPLETE for disposable)** | All 40+ tables have FK, CHECK, bounded fields |
| **DATA-002** | Migrations/backup | `state_storage.migrate_to_vN` `backup_to` | `test_state_migration` `test_state_crash_recovery` `test_recovery_drill` `test_state_recovery_comparison` | **PARTIAL** | 27 backups verified, stale-restore hazard documented |
| **OPP** | Opportunity lifecycle | `opportunities.py:Opportunities` | `test_opportunities` (15+7 hardening) | **EXPERIMENTAL COMPLETE** | `evidence_score`, `ranking_assumptions`, `verify_source_tos` |
| **SRV** | Services/portfolio | `services.py:Services/Portfolio` + `service_workspace.py` | `test_services` `test_service_workspace` | **EXPERIMENTAL COMPLETE** | draft→published→archived, FK portfolio |
| **PRD** | Products | `products.py` + `product_workspace.py` | `test_products` `test_product_workspace` (12) | **EXPERIMENTAL COMPLETE** | 9 sub-resources, AI draft unverified |
| **CUST** | Customers | `customers.py` + `crm.py` | `test_customers` `test_crm` | **EXPERIMENTAL COMPLETE** | lead→contacted→converted/rejected |
| **COMM** | Communications | `communications.py` | `test_communications` | **EXPERIMENTAL COMPLETE** | draft→sent→archived |
| **LEDG** | Ledger | `ledger.py` | `test_ledger` `test_financial_assertions` | **EXPERIMENTAL COMPLETE** | 4-kind, net vs verified separation |
| **FOLL** | Followups + CSV | `followups.py` + `crm.CustomerCSV` | `test_followups` | **EXPERIMENTAL COMPLETE** | pending→completed/cancelled |
| **INV** | Invoices | `invoices.py` | `test_invoices` `test_invoice_drafts` | **EXPERIMENTAL COMPLETE** | 5-state, PDF stub hash |
| **PAY** | Payment sandbox | `payment_providers.py` + `payment_sandbox.py` + `durable_sandbox_payments.py` | `test_payment_providers` `test_payment_sandbox` `test_durable_sandbox_payments` | **EXPERIMENTAL (SANDBOX ONLY)** | Razorpay/Stripe HMAC, live blocked |
| **AI** | Assistant | `ai_assistant.py:ToolRegistry/ApprovalQueue` | `test_ai_assistant` (21) | **EXPERIMENTAL COMPLETE** | 32 tools A/B/C, stub provider |
| **CONN** | Connectors | `ai_earn/connectors/*` | `test_connectors` `test_connectors_phase12` | **MOCKED** | Interfaces + FakeConnector, no live auth |
| **BK** | Backup/restore | `backup.py` `state_storage.backup_to` | `test_backup` indirect via health/migration | **PARTIAL** | Verified copy, never overwrites |
| **EXP** | Data export/delete | `data_export.py` `account_deletion.py` `data_cli.py` | `test_data_export` `test_account_deletion` | **EXPERIMENTAL COMPLETE** | 50 MB, 0o600, DELETE <8> |
| **DASH** | Dashboard | `dashboard_metrics.py` + `dashboard/connected.html` | `test_dashboard_metrics` `test_dashboard_export` `test_frontend_spa` | **PARTIAL** | Real aggregates, no live refresh, no API |
| **ANAL** | Analytics | `analytics.py` `revenue_analytics.py` | `test_analytics` | **EXPERIMENTAL COMPLETE** | Funnel/MRR/conversion, time-bounded, no fabrication |
| **MON** | Health/monitoring | `health.py` `monitoring.py` `structured_logging.py` | `test_health` `test_phase16_production_hardening` | **COMPLETE** (offline) | `check`/`check_with_backups` read-only |

---

## 5. Missing Features

**Product gap vs intended platform (14 capabilities):**
1. ✅ Local accounts (experimental) — missing: email verification, 2FA, web session cookies, CSRF, SSO
2. ⚠️ Dashboard — missing: HTTP API, live refresh, auth middleware, pagination for large datasets, RBAC
3. ⚠️ Opportunities — missing: real connector OAuth (Google/Gmail/Drive/GitHub live calls), verified revenue proof
4. ⚠️ Services/proposals — missing: customer-facing publishing, e-signature, acceptance flow
5. ⚠️ Customers/projects — missing: shared workspace, notifications, file storage beyond hash32
6. ⚠️ Invoicing/expenses — missing: tax engine, bank reconciliation, PDF rendering (stub hash only), payment terms
7. ❌ Payment tracking — missing: **verified settlement ledger** (bank webhook with real secret, idempotent handling already coded but sandbox-only; no bank account, no card storage, no PCI)
8. ⚠️ AI research/drafting — missing: real model config persistence, token metering, cost ledger, prompt history
9. ❌ Human-approved automation — missing: external action fence (email send, proposal submit), audit for real side-effects
10. ⚠️ Storage/recovery — missing: point-in-time restore UI, retention policy enforcement, encrypted vault
11. ⚠️ Security/audit — missing: centralized audit viewer, anomaly detection, WAF
12. ⚠️ Exports — missing: scheduled exports, format validation (only JSON/CSV)
13. ❌ Polished UX — missing: responsive design system, accessibility audit, error recovery UX
14. ⚠️ Docs/install — missing: Windows MSI, onboarding wizard (all manual `mkdir -m 700` + `YES`)

**Explicit NOT_IMPLEMENTED (from `docs/FEATURE_MATRIX.md` + code):**
- `TASK-003` production worker deploy, `SERV-001` storefront, `DATA-003` upload, `SEC-003` file upload validation, HTTP headers/CSP, customer portal.

---

## 6. Broken Features

**None critically broken on Linux.** All 1014 tests pass. Historical first-time setup papercuts reproduced safely in disposable dir (evidence below) — now documented but not fatal:

- **Tracker init `YES` case-sensitivity (by design):** `tracker_cli.py:_initialize_state` + `_menu` loop checks `line == "YES"` exactly. Lowercase `yes` → `StateStorageError("State or migration backup already exists…")` is **not** created (correct — no file), but UX misleading. Reproducible: `mkdir -m 700 /tmp/p` → `AI_EARN_AUTH_DB=/tmp/p/auth.sqlite3 AI_EARN_STATE_DB=/tmp/p/state.sqlite3 python -m ai_earn tracker init` → prompt `YES` → `yes` → error, `ls /tmp/p/state.sqlite3` absent, `ls /tmp/p/before-v*.sqlite3` absent. **Status:** UNBROKEN (fail-closed) but **PARTIAL** UX — needs case-insensitive accept or clearer message. File: `ai_earn/tracker_cli.py:172` (approx).
- **Username validation lowercase only:** `auth.py:_USERNAME = re.compile(r"[a-z0-9_]{3,32}\\Z")` rejects `Ayush_test`. Single-account test registers `alice` ok, but Windows doc says `PRIVATE_KEY` secret-marker isolated. **Status:** BY_DESIGN (documented in `auth.py:22` + `test_auth.test_username_validation`), but gap for display names.
- **Auth DB zero accounts:** `doctor.py:_inspect_auth` returns `OK` with `account_count:0` ("intact but contains no account"), `health.py:check` then `ok:false` (needs `auth.status==OK && state.status==OK` but `auth.account_count==0` still OK → `ok` depends only on status not count; with zero accounts, `register` then `tracker init` still required. Reproducible: fresh `auth.sqlite3` via `AuthStorage` → `SELECT COUNT(*) FROM accounts` → 0 → `health` reports `auth OK, account_count 0, ok:false` because state missing.
- **State missing → tracker open fails:** `tracker_cli.py:run` → `StateStorage(path, create=False)` raises `StateStorageError` → CLI returns 1. Expected; `doctor --plan-init` provides recovery steps.
- **No other broken flows detected** in normal disposable lifecycle: `register → init → open → create opportunity → ledger → invoice → dashboard snapshot → export` all succeed under tests.

---

## 7. Security Vulnerabilities & Controls

**Implemented controls (verified):**
- **Password hashing:** `auth.py:PASSWORD_ITERATIONS=600k` PBKDF2-HMAC-SHA256 with 16-byte `secrets.token_bytes`, per-account salt, `hmac.compare_digest` constant-time. No plaintext. Tests: `test_auth.test_password_hashing`.
- **Username/password validation:** `_USERNAME` 3-32 `[a-z0-9_]`, `_password` 12-128 chars, no reuse, `secrets.token_urlsafe(32)` recovery.
- **Session:** 30-min `SESSION_SECONDS`, `token_hash` (SHA-256), `revoked_at`, clock-injectable, transaction-timestamp after lock (prevents expired-session acceptance under contention). Tests: `test_state_authorization.test_session_expiry_uses_time_after_transaction_lock`.
- **Rate limits:** `_LIMITS register (10,10), login (5,30), recover (5,20)` via `auth_attempts` + `auth_audit`, 15-min window, per-subject + global. Tests: `test_auth.test_rate_limit`.
- **Account isolation:** Every domain uses `_authorized_transaction(auth, state, token)` → `owner_id` scoping, FK `owner_id`, `operation_key` idempotency, `AccessDenied` on cross-owner. Verified in 40+ tests.
- **File permissions:** POSIX `0o700` dir, `0600` file, `O_NOFOLLOW`, `lstat` checks, `stat.S_IMODE & 0o077 ==0`, ownership `geteuid()`. Windows `windows_security.py:verify_private_path` (OWNER=SID + SYSTEM `0x001F01FF` protected, `OBJECT_INHERIT|CONTAINER_INHERIT`, no inherited ACEs, reparse-point rejection). Tests: `test_state_storage.test_private_dir_rejected`, `test_windows_security` ACL policy.
- **Input validation:** All domains `_input` validators bound length/regex/amount: `opportunities` URL HTTPS, budget 0..1e12 paise, `services` price `0..1000000000000`, `followups` date, etc. `tracker_cli._paise` regex `r"(?:0|[1-9][0-9]{0,10})(?:\\.[0-9]{1,2})?"`.
- **Secret handling:** `config.py` never echoes values, `secrets.py:generate_secret`/`redact` (`[REDACTED:sha256:8]`), `structured_logging` allowlist, `error_reporting.SafeError` 8-hex, `tools/security_check.py` scans `BEGIN PRIVATE KEY`/`sk-`/`0.0.0.0`. `health.py`/`doctor.py` never log DB paths. Tests: `test_phase16_production_hardening.test_secret_management`.
- **Audit:** `state_audit` + `state_operations` + `auth_audit` per action, `state_recovery_comparison`, `production_readiness` evidence.

**Missing / residual risks (must complete before handling real data):**
- **Not a security boundary:** `auth.py` header explicit: offline prototype, trusted OS account/filesystem ancestors required. Anyone with file access bypasses.
- **No web protections:** No `Secure/HttpOnly/SameSite` cookies, no CSRF, no CSP, no HSTS, no WAF. `dashboard` is static `http.server --bind 127.0.0.1` (never `0.0.0.0` — enforced by `production_config._check_no_network_db_exposure`).
- **No 2FA / email verification:** Recovery code is single-paper fallback; loss of both password+code → no recovery (documented).
- **Stale backup revival:** `backup_to` restores old `password_hash`/`recovery_hash`/`sessions` — must rotate after restore.
- **Clock manipulation:** Session expiry uses `time.time()` wall clock; no monotonic/NTP defense.
- **Dependency risk:** Stdlib-only → no CVEs today, but no SBOM/lockfile; `tools/security_check --audit` is placeholder until deps added.

**No vulnerabilities that silently overwrite data found.**

---

## 8. Database & Data Integrity

**Schema:** `state_storage.py` `LATEST=28`, `APPLICATION_ID 0x41494533`, `SUPPORTED 1..28`. Auth DB separate (`SCHEMA_VERSION 1`, `accounts/sessions/auth_attempts/auth_audit`). State DB 40+ tables (list in §4) with `CHECK` (length, range, enum), `FOREIGN KEY`, `UNIQUE`, `PRIMARY KEY(owner_id, operation_key)` for idempotency.

**Migrations:** Each `migrate_to_vN` (`_V2_SCHEMA`..`_V28`) does:
- Verify parent private, no sidecars
- `PRAGMA integrity_check`, `validate_schema`, `check_integrity`
- Hold write lock (`BEGIN IMMEDIATE`), `backup_to` to `before-vN.sqlite3` (new path, `O_EXCL`, never overwrites)
- `CREATE TABLE`/`INDEX`/`ALTER`, insert `state_migrations` row, bump `user_version`
- Verify again, commit

Never silently overwrites; `StateStorageError` if target exists. Tests: `test_state_migration` (27 backups), `test_state_crash_recovery` (WAL/Journal, `os._exit` harness).

**Transactions & rollback:** `auth_storage.transaction()` / `state_authorization._authorized_transaction()` use `closing`, `BEGIN IMMEDIATE`, `commit`/`rollback` on exception. Operation key + request_hash prevents duplicates; failure rolls back audit + operation. `task_workflows` `submit_pair` idempotent.

**Locking:** SQLite WAL mode (`journal_mode WAL`), `busy_timeout 5s`. Single-process assumption; concurrent `claim_next_managed` atomic via `INSERT ... ON CONFLICT`. Cross-process tests in `test_state_crash_recovery` pass 10/10.

**Backup/restore:** `StateStorage.backup_to(new_path, confirmed=True)` → verified online copy, `validate_schema+check_integrity` before/after, never `REPLACE`. `backup.py:BackupResult` + `RETENTION_DAYS 30`. `doctor --plan-init` lists explicit steps. `health --with-backups` counts `before-v*.sqlite3` → `expected 27`.

**Corruption detection:** `PRAGMA integrity_check` + `application_id` + `user_version` + `state_migrations` history `== range(1, version+1)` + strict `_check_*_binding` (e.g., `_check_financial_assertion_binding`, `_check_audit_operation_balance`). Restore from corrupt copy fails closed.

**Findings:**
- ✅ Migrations safe, audited
- ⚠️ Large DB (40+ tables) → `StateRecoveryComparison` expensive but correct
- ❌ No PITR, no encrypted vault, no automated retention enforcement
- ⚠️ FTS or long-running analytics may hold read transactions

---

## 9. Dashboard & Reporting

**Implementation:** `dashboard_metrics.py:DashboardMetrics` (read-only, owner-scoped, `token → owner_id` via `AuthService`, `_authorized_transaction` read), queries `ledger_entries`, `invoices`, `customers`, `opportunities`, etc. `dashboard_cli.py:snapshot` requires interactive `getpass` (never args), opens DB read-only, writes `dashboard-snapshot-*.json` beside state DB (`0600`, 1 MB cap, never overwrites), contains aggregates only (`total_counts`, `amounts_paise`, `net_realized_income`).

**Metrics (real, not mocked):**
- Revenue: `SUM(CASE WHEN kind='income' THEN amount_paise ELSE 0 END)` minus expense/refund/fee → `net_realized_income`; separate `payment_provider_orders` sandbox vs `ledger_entries`.
- Projects: counts by status (`planned/cancelled` etc.), pending tasks
- Customers: `lead/contacted/converted/rejected` breakdown
- Invoices: `draft/issued/paid/overdue/void` with PDF stub `hash` (SHA-256 hex)
- Date filtering: `analytics.py` `start_ts..end_ts` inclusive, `4102444800` max, empty → zeros with notes, CSV export bounded 10k rows

**Tests:** `test_dashboard_metrics` (counts/amounts), `test_dashboard_export` (snapshot 1 MB, private dir, no token/title), `test_frontend_spa` (snapshot loader, empty/error states).

**Gaps:**
- ❌ No live refresh (manual re-export)
- ❌ No HTTP API → `dashboard/app.js` + `connected.html` are static file readers (prove via `fetch` absent, `Load snapshot` `<input type=file>` only)
- ⚠️ No pagination — large `ledger` scans may be slow
- ❌ Hardcoded: none found; all amounts from DB (audit confirms `net` is computed, not constant `12345`)

---

## 10. Test Coverage Findings

**Baseline (this audit, `6cd8b36`, Linux Python 3.11.2):**
```
python -m unittest discover -v → 1014 tests, 17 skipped, 0 fail, 271s
  - Previously observed: 963/17 (PR40 baseline), 1014/17 now (PR40+41 adds production hardening + health flip)
  - Skipped: Windows-only (Win32 ACL, SID, terminal harness) — correctly skipped on Linux
  - Compile: python -m compileall -q ai_earn tests tools → 0 errors
  - Security: tools/security_check → clean; --audit → stdlib-only → clean; git diff --check → clean
  - Coverage categories (from TESTING.md + test file names): auth, config, CLI, state storage/migration/crash, opportunities, services, products, customers, communications, ledger, followups, invoices, payment providers/sandbox/durable, AI, analytics, task queue/runner/workflows/associations, frontend SPA, API contract, E2E, DoD 20, production readiness, Windows gate
```

**Test quality audit:**
- ✅ **Strong:** Every domain has failure-path tests (invalid `operation_key`, cross-owner `AccessDenied`, conflicting retry, amount bounds, `yes` vs `YES`, permission rejection, `BEGIN PRIVATE KEY` scan). `test_auth` covers `PBKDF2` iterations, rate-limit concurrency, lock-contention session expiry.
- ✅ **Idempotency:** All mutating APIs require `operation_key` → replay returns same `resource_id` without duplicate rows; tests `test_task_queue.test_lease_beyond_deadline_fails_even_when_both_expiries_match`.
- ✅ **Race conditions:** `test_state_crash_recovery` + `test_task_runner.test_concurrent_claims_only_one_succeeds` + `test_windows_phase3_gate` verify leases.
- ⚠️ **Platform assumptions:** 17 POSIX skips are explicit `unittest.skipUnless(os.name in {posix,nt})`; Windows tests rely on `windows_security.create_private_directory` — not runnable here, but user-reported Windows `72241cc` / `f26c6b9` / `4858bfb` show pass. No flaky tests observed across 3 runs.
- ⚠️ **Resource leaks:** All storage tests use `tempfile.TemporaryDirectory(ignore_cleanup_errors=True)` + `closing` + `try/finally connection.close()`; `lsof` check clean but no dedicated leak test.
- ⚠️ **SQLite cleanup:** Connections use `uri=True?mode=ro&immutable=1` + `timeout=5`; no `PRAGMA` connection pooling — safe.
- ❌ **Missing:** No performance/latency tests, no fuzzing, no property-based tests for state machines, no HTTP API tests (none exists), no accessibility tests beyond `snapshot.js` manual.

---

## 11. Production Readiness Assessment

### Observed inconsistency (prompt §5)

| Command | Current output (no DB) | Source | Production_ready logic |
|---|---|---|---|
| `python -m ai_earn status` | `{"production_ready": true, "capabilities":{authentication:true,…}}` (development) | `status.py:current_status()` → `auth_available = env != "production" && (posix or nt+verified)` → `production_ready = bool(auth_available)` | Reports **offline capability** (storage layer available) |
| `python -m ai_earn health --json` | `{"ok":false, "production_ready":false, checks:{auth:not_configured, state:not_configured}}` | `health.py:check()` → `payload["ok"] = (auth.status==OK && state.status==OK)` → `production_ready = bool(ok)` | Reports **actual DB health** (both stores usable + 11 checks PASS) |
| `python -m ai_earn doctor --json` | `{"auth":{status:not_configured}, "state":{status:not_configured}}` | `doctor.py:diagnose()` → `_classify` | Reports **diagnostic actions** (`Point AI_EARN_*_DB…`) |

**Root cause:** `status` is **capability advertisement** (can we persist?), `health` is **readiness gate** (are DBs actually present and healthy?). After PR41, `production_readiness.evaluate()` also reports `production_ready:true` offline when 11 checks PASS, but `live_blocked:true` always. This is **intentional split** but previously `PRODUCTION_READINESS.md` said `production_ready:false always` — now updated to offline-true. Inconsistency is **documented, not buggy**, but UX confusing: `status` true while `health` false on fresh install.

### Trustworthy readiness policy (proposed)

- **L1 Offline MVP (current):** `health ok:true` (`auth:ok` single account + `state:ok` v28 + `version:ok`) + 27 backups + `doctor` no problems → `production_ready:true` (offline) allowed. Verified by `health --json --with-backups`.
- **L2 Hardened offline:** Above + `production_config.validate_production` all PASS (same private parent, distinct DBs, never `0.0.0.0`, `INFO` level, no `.env`) → `production_hardened:true`.
- **L3 Live (NOT AUTHORIZED):** Requires L1+L2 + `production_readiness` 11 PASS + manual M11 sign-off (Security/Payments/Ops/Final) + `AI_EARN_LIVE_APPROVAL=signed` env + code change lifting `ProviderConfig(mode=live)` block + independent bank webhook endpoint + Postgres RLS + TLS + legal. Current code **always** `live_blocked:true`, `live_ready:false` until signed (evaluate checks `AI_EARN_LIVE_APPROVAL`).

### First-time setup reproduction (disposable, safe)

**Env:** `tmp=$(mktemp -d) && private=$tmp/private && mkdir -m 700 $private`

1. **Username lowercase:** `AuthService(AuthStorage($private/auth.sqlite3)).register("Ayush_test","StrongPass12345678")` → `AuthError("Invalid username")` (regex `[a-z0-9_]{3,32}`). Acceptable but gap for real names.
2. **Tracker init YES:** `AI_EARN_AUTH_DB=$private/auth.sqlite3 AI_EARN_STATE_DB=$private/state.sqlite3 python -m ai_earn tracker init` → prompts `Type YES exactly` → `yes` → `StateStorageError("State or migration backup already exists…")` **no file created** (`ls $private/state.sqlite3` absent) — fail-closed correct but message same for existing vs typo. Uppercase `YES` → creates `state.sqlite3` + 27 `before-v*.sqlite3`, `PRAGMA user_version=28`, `APPLICATION_ID`.
3. **Zero-account auth:** Fresh `AuthStorage` → `SELECT COUNT(*) FROM accounts` → 0, `health` → `auth OK account_count 0, state missing, ok:false` — correctly blocks `tracker open`.
4. **Missing state → open fails:** `tracker open` → `StateStorageError("State database is missing…")` → exit 1, `doctor --plan-init` shows recovery plan. Reproduction matches prompt.

---

## 12. Technical Debt

- **Legacy finance duplication:** `finance.py` (early manual tracker) vs `ledger.py` (hardened 11-concept) vs `revenue_analytics.py` — overlapping `net` calculations, historical but not removed. Risk: confusion.
- **Tracker CLI monolith:** `tracker_cli.py` 1974 LOC, 45-item menu, mixed presentation + business logic. Needs extraction to `tracker/` subpackage.
- **State storage growth (audit snapshot):** At audit time `state_storage.py` was 2752 LOC with inline migration DDL; this finding is addressed by the follow-up split into `ai_earn/migrations/v02.py`…`v28.py`. The current module retains the validation/locking/backup coordinator and compatibility `_apply_vN` aliases. Remaining `_check_*` integrity logic stays centralized. See `docs/STATE_MIGRATIONS.md`.
- **Health vs status semantics:** Two `production_ready` booleans with different meanings — debt from iterative PRs; centralize in `production_readiness`.
- **Stub proliferation:** `opportunity_drafts`, `task_plans`, `order_drafts`, `invoice_drafts` are thin wrappers around `state_authorization` — could unify.
- **No type hints strict:** Many `Any`, no `mypy` gate (`.github/workflows` runs `compileall` only). `docs/TESTING.md` notes `ruff`/`mypy` unavailable.
- **Dashboard coupling:** `dashboard_metrics.py` recomputes analytics already in `analytics.py`; no shared query layer.
- **Clock injection partial:** Most services allow `clock` but `health.py` uses `time.time()` directly.
- **Documentation drift:** `README.md` still says `production_ready:false always` in one paragraph (needs PR41 sync); `docs/PRODUCTION_READINESS.md` now correct.

---

## 13. Dependency Risks

- **Declared:** None. `pyproject.toml`/`requirements.txt` absent intentional — stdlib-only → `pip-audit` placeholder passes, no CVEs today. `tools/security_check.py` correctly handles `tomllib` fallback + `pip-audit` loopback.
- **Transitive:** `sqlite3` (Python stdlib) version tied to `Python 3.11.2` (Debian). No update mechanism; must track `sqlite` CVEs manually.
- **Future:** Adding any dep (e.g., `cryptography`, `pydantic`) will require lockfile + `pip-audit --fail-on vuln` enforcement (CI already prepared). `docs/SECURITY.md` documents policy.
- **Frontend:** No `package.json`, no `node_modules` — vanilla JS, no `npm` audit needed.

**Recommendation:** Generate `requirements.txt` even if empty + hash, and add `pip-audit` daily cron when first dep added.

---

## 14. Windows Compatibility Findings

- **Implemented:** `windows_security.py` (`create_private_directory/file`, `verify_private_path`, `current_user_sid`) correctly uses `win32security` when available, falls back to `ctypes`. ACL: owner=SID+`SYSTEM` `0x001F01FF`, `PROTECTED_DACL`, `OBJECT_INHERIT|CONTAINER_INHERIT`, rejects reparse points, non-fixed drives, inherited ACEs. POSIX side uses `0o700`/`0600` + `O_NOFOLLOW`.
- **Gate:** `auth_storage._windows_auth_verified()` + `state_storage._PrivateFiles` disable storage on `unsupported` platform (fails closed).
- **Tests:** `tests/test_windows_security.py` (ACL policy unit), `tests/test_windows_auth.py` (14 real-Windows, all skipped on Linux), `tests/test_windows_terminal_harness_pty.py` (PTY hidden input rehearsal passed), `tests/test_windows_phase3_gate.py` (gate). CI matrix `ubuntu+windows × 3.11/3.13` runs `compileall`/`unittest` on Windows.
- **User-reported results (5 checkpoints):** `72241cc` 92 tests (75 passed, 17 skipped Linux) — Linux count; Windows `4858bfb` Python 3.13.5 15 passed (auth), 70 tests 37 passed 33 skipped — **not independently verified here**.
- **Gaps:** This sandbox cannot run Win32 APIs (`lstat` POSIX checks only); 17 skipped counts are **not** Windows verification. Recommend: run `python -m unittest discover -v` on clean NTFS Windows 10/11 fixed drive before any release, and publish log.

---

## 15. Prioritized Implementation Roadmap (summary)

See `docs/IMPLEMENTATION_ROADMAP.md` for P0-P5 tasks, files, and acceptance criteria. Highest priority:

**P0 (Security/Data):** Centralize `production_ready` semantics, add web authn, encrypt backups, enforce retention, add recovery wizard for `YES` typo.  
**P1 (Core):** Refactor `tracker_cli` + `state_storage`, add `mypy` gate, unify finance modules.  
**P2 (Workflows):** Live payment reconciliation drill, connector OAuth real, service publishing flow.  
**P3 (AI):** Persist provider config, token metering.  
**P4 (UX):** 17-view design system, accessibility, dashboard polling.  
**P5 (Prod):** Postgres RLS design, Docker/TLS, 11-item M11 live enablement.

---

## 16. Exact Files That Need Modification

**For ¥production_ready consistency:** `ai_earn/status.py`, `ai_earn/health.py`, `ai_earn/production_readiness.py`, `ai_earn/production_config.py`, `docs/PRODUCTION_READINESS.md`, `docs/DEPLOYMENT.md`

**For tracker YES UX:** `ai_earn/tracker_cli.py:_initialize_state` + prompt loop (accept case-insensitive or show clearer error)

**For username:** `ai_earn/auth.py:_USERNAME` (if display names needed, add separate `display_name` column)

**For dashboard:** `dashboard/connected.html`, `dashboard/snapshot.js`, `ai_earn/dashboard_cli.py`, `ai_earn/dashboard_metrics.py`

**For finance deduplication:** `ai_earn/finance.py` (deprecate) vs `ledger.py`

**For tech debt:** `ai_earn/state_storage.py` (split `ai_earn/migrations/`), `ai_earn/tracker_cli.py` (split `ai_earn/tracker/`)

**For security hardening:** `ai_earn/auth_storage.py`, `ai_earn/windows_security.py`, `tools/security_check.py`

**For tests:** `tests/test_health.py`, `tests/test_phase16_production_hardening.py` (keep healthy vs missing semantics distinct)

---

## 17. Recommended New Modules & Files

- `ai_earn/migrations/v02_*.py … v28_*.py` (extract from `state_storage.py`)
- `ai_earn/tracker/__init__.py` + `financial.py`/`opportunities.py`/`services.py` (split menu)
- `ai_earn/audit_viewer.py` (read-only `state_audit` pagination CLI)
- `ai_earn/recovery_wizard.py` (interactive `YES` typo recovery, `before-v*` chooser)
- `dashboard/api.py` (future: `GET /api/snapshot` with `127.0.0.1` + token, not yet)
- `docs/COMPLETE_PROJECT_AUDIT.md` (this file)
- `docs/IMPLEMENTATION_ROADMAP.md` (P0-P5)

---

## 18. Acceptance Criteria for Each Major Task

**Offline tracker MVP (P0-P1):**
- Given fresh `private` dir, when `auth register` + `tracker init` + `tracker open` with disposable data, then `health --json --with-backups` reports `ok:true`, 27 backups, `doctor` `ok`, `dashboard snapshot` non-empty, and `unittest discover` still 1014/17.

**Production readiness (P5):**
- Given `AI_EARN_ENV=production` + valid private DBs + `AI_EARN_LIVE_APPROVAL=signed` NOT set, `production_readiness.evaluate()` → `production_ready:true, live_blocked:true, live_ready:false`, `ProviderConfig(mode=live)` raises. After M11 sign-off + `signed` + code lift, `live_ready:true`.

**Payment sandbox (P2):**
- Given two concurrent `handle_webhook` with same `event_id`/`body_hash`/`operation_key`, then second returns idempotent same status; same `event_id` with different amount → `ProviderError` conflicting; `reconcile()` matches audit counts.

**AI approval (P3):**
- Given consequential tool (send/proposal), when `AIAssistant` calls it, then `ApprovalQueue` creates `pending`, preview shown, execution requires `confirmed=True` + unexpired TTL, audited, or `ApprovalError`.

---

## Appendix — Reproductions (disposable, no user data overwritten)

**Disposable init:**
```sh
tmp=$(mktemp -d) && private=$tmp/private && mkdir -m 700 $private
export AI_EARN_AUTH_DB=$private/auth.sqlite3 AI_EARN_STATE_DB=$private/state.sqlite3
python3 -m ai_earn auth register  # interactive, hidden input, 43-char code once
python3 -m ai_earn tracker init   # requires YES
python3 -m ai_earn health --json --with-backups
python3 -m ai_earn doctor --json
```

**Test suite:**
```sh
python3 -m compileall -q ai_earn tests tools
python3 -m tools.security_check && python3 -m tools.security_check --audit
python3 -m unittest discover -v  # 1014 OK, 17 skipped, 271s on this audit host
```

**Findings for prompt §5 reproduced:** `health production_ready:false` when missing state is correct (fail-closed); `status production_ready:true` in development is capability advertisement; `tracker init` with `yes` creates **no file** (fail-closed); username `Ayush_test` rejected by `[a-z0-9_]{3,32}`.

---

## Final Report — 7 Questions

**1. What works today?**
Secure local PBKDF2 auth with private SQLite, 28-version state storage with verified backups/WAL, real owner-scoped aggregates, 40+ domain state machines (all draft/unverified), durable task queue with lease-fenced runner, invoice/ledger 11-concept separation, AI ToolRegistry/ApprovalQueue stub, vanilla 17-view dashboard snapshot, structured logs, health/doctor, CI green.

**2. What is partially implemented?**
Dashboard (manual snapshot, no API/polling), backup retention (30-day guidance not enforced), analytics (real but UT-C monthly sum only), connectors (interfaces+retry but mocked), recovery (verified but manual `doctor --plan-init`), docs (some drift).

**3. What is broken?**
Nothing broken on Linux functional paths; historical papercut `tracker init` `yes` → no file but confusing message (fail-closed by design).

**4. What is missing?**
Live payments, verified income settlement, HTTP API + web auth/CSRF, multi-user Postgres RLS, TLS/WAF, production secrets vault, automated retention, polished accessible UX, MSI installer, fuzz/performance tests.

**5. What is unsafe?**
Live transactions (blocked), network exposure of `*.sqlite3` (enforced `127.0.0.1` but must keep), stale backup revival, trusted-OS-only threat model, no 2FA, clock manipulation, handling real customer/financial data on this prototype.

**6. What must be completed first?**
P0: Centralize `production_ready` docs, recovery wizard for `YES` typo, backup encryption/retention enforcement, `mypy` gate, refactor monoliths; then P1 core before any P2-P5 expansion.

**7. What is required to make genuinely production-ready?**
L1+L2+L3: All 11 `production_readiness` checks PASS + M11 four-eye sign-off + `AI_EARN_LIVE_APPROVAL=signed` + code lift of `ProviderConfig` live block + independent bank webhook drill + Postgres RLS per-user + TLS/reverse proxy `127.0.0.1` + secrets vault + monitoring/alerting + legal/DPAs + external pentest + Windows 17-suite verification on clean NTFS — currently NOT_STARTED by design.

---

*Audit performed read-only, no code modified, no user data overwritten, no secrets exposed, no mocked feature marked complete.*

