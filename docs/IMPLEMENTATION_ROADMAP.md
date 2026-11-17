# Implementation Roadmap — Ai-earn

**Date:** 2026-11-17  
**Branch:** `arena/4bd4230e-ai-earn` from `6cd8b36` (main, PR41 offline-ready)  
**Audit:** `docs/COMPLETE_PROJECT_AUDIT.md` (442 LOC, 1014 tests/17 skipped, 271s)  
**Principles:** No code modified in audit; stdlib-only; disposable-data prototype; `live_blocked:true` until M11 sign-off; Windows NTFS ACL + POSIX 0600 remain.

---

## Execution Order

`P0` must close before handling real customer/money. `P1` must close before `P2` expansion. `P3`/`P4` can overlap `P2`. `P5` is final gate.

```
P0 (Critical) → P1 (Core) → P2 (Business) ─┬─→ P3 (AI) ─┐
                                            └─→ P4 (UX)  ├─→ P5 (Prod)
```

---

## P0: Critical Security & Data Integrity

### P0-1 Centralize `production_ready` semantics
- **Problem:** `status.py` (capability) vs `health.py` (health) vs `production_readiness.py` (11 checks) vs `production_config.py` (hardening) each compute `production_ready` differently → confusing `status:true` / `health:false` on fresh install.
- **Files:** `ai_earn/status.py`, `ai_earn/health.py`, `ai_earn/production_readiness.py`, `ai_earn/production_config.py`, `docs/PRODUCTION_READINESS.md`, `docs/DEPLOYMENT.md`, `tests/test_health.py`, `tests/test_cli.py`
- **Task:** Introduce `ai_earn/readiness.py` single source `def overall(environ) -> {offline_ready, live_blocked, live_ready, reason}` that composes `evaluate` + `validate_production` + `check`. `status`/`health` delegate. Docs single table.
- **Acceptance:** `status`/`health` `--json` both contain `readiness:{offline_ready, live_blocked, live_ready}` identical for same disposable DB; tests `test_api_contract` + `test_health` green; no `production_ready` drift.

### P0-2 Tracker `YES` recovery wizard
- **Problem:** `tracker_cli.py:_initialize_state` requires exact `YES`; typo `yes` → no file but message same as existing DB (`StateStorageError` generic).
- **Files:** `ai_earn/tracker_cli.py` (prompt loop ~L172), `ai_earn/state_storage.py`, `tests/test_tracker_cli.py`
- **Task:** Case-insensitive accept with confirmation (`YES/yes` + second `confirm YES`) or distinct errors: `TTY typo: expected YES` vs `already exists`. Add `ai_earn/recovery_wizard.py` chooser for `before-v*`.
- **Acceptance:** Disposable test: `echo yes | tracker init` → `Did you mean YES? [y/N]` and no file; `echo YES | tracker init` → 27 backups; existing DB → `already exists, use --recovery-wizard`.

### P0-3 Backup encryption & retention enforcement
- **Problem:** `backup.py:RETENTION_DAYS=30` guidance only; `dashboard-snapshot-*.json` + `*.sqlite3` are plaintext 0600.
- **Files:** `ai_earn/backup.py`, `ai_earn/state_storage.py:backup_to`, `ai_earn/secrets.py`, `tools/security_check.py`
- **Task:** Add `encrypt_backup(path, key_id)` (age/libsodium or OS keychain stub, not `cryptography` yet without deps) + `prune_backups(keep=27+7)` with dry-run. Document retention in `docs/BACKUP_AND_RECOVERY.md`.
- **Acceptance:** `backup_to` + `encrypt` → `.bak.age` verified by `integrity_check` after decrypt; `prune` never deletes `before-v*.sqlite3` chain, only `*.backup-*`; never overwrites.

### P0-4 Stale backup revival mitigation
- **Files:** `ai_earn/auth_storage.py`, `ai_earn/state_storage.py`, `docs/SECURITY.md`
- **Task:** On `backup_to` restore to new path, force password rotation (`replace_recovery_code` + session revocation) and emit warning. Add `doctor --check-stale` comparing `auth_audit` timestamps.
- **Acceptance:** Restored stale copy → first login succeeds but next `health` warns `stale` and `replace_recovery` required; test `test_state_crash_recovery.test_stale_backup_revives_sessions` updated.

### P0-5 Username display name separation
- **Files:** `ai_earn/auth.py:_USERNAME`, `ai_earn/auth_storage.py:accounts`, `tests/test_auth.py`
- **Task:** Keep `_USERNAME` `[a-z0-9_]{3,32}` for login, add `display_name TEXT CHECK(length 1..64)` for UI; never use display for isolation.
- **Acceptance:** Register `alice` + `display_name="Ayush Test"` succeeds; login still `alice`; `display_name` appears in `dashboard snapshot` only, not in `owner_id`.

---

## P1: Core Application Functionality

### P1-1 Split `tracker_cli` monolith
- **Files:** `ai_earn/tracker_cli.py` (1974 LOC) → `ai_earn/tracker/{__init__.py, menu.py, financial.py, opportunities.py, services.py}`
- **Task:** Extract 45-item menu into modules, keep `run()` dispatch; no behavior change.
- **Acceptance:** `unittest discover` still 1014/17, `tracker open` manual test unchanged.

### P1-2 Split `state_storage` migrations
- **Files:** `ai_earn/state_storage.py` (2752 LOC) → `ai_earn/migrations/v02.py … v28.py` + `state_storage.py` loader
- **Task:** Each `migrate_to_vN` in own file with `_V*_SCHEMA`; `MIGRATIONS` registry. Keep `APPLICATION_ID`/`LATEST`.
- **Acceptance:** Fresh `StateStorage(create=True)` → v28 + 27 backups; `migrate_to_v28` still holds lock + backup.

### P1-3 Unify finance pipeline
- **Files:** `ai_earn/finance.py` (legacy) vs `ledger.py`/`revenue_analytics.py`
- **Task:** Deprecate `finance.py` → wrapper to `ledger.py`; reconcile `net_realized_income` single function.
- **Acceptance:** `FinanceError` deprecated warning, tests `test_finance` alias still green, `dashboard_metrics` and `analytics` share query.

### P1-4 `mypy` + `ruff` gates
- **Files:** `.github/workflows/tests.yml`, `pyproject.toml` (new, empty deps), `ai_earn/*.py` type hints
- **Task:** Add `ruff check` and `mypy --strict` (allow `Any` in DB layer initially) to CI; fix 0 errors.
- **Acceptance:** CI job fails on lint/type, not just `compileall`; 1014 tests still green.

### P1-5 Config `readiness` single source (extends P0-1)
- **Acceptance:** As P0-1.

---

## P2: Business Workflows

### P2-1 Verified settlement (not sandbox)
- **Files:** `ai_earn/payment_providers.py`, `ai_earn/payment_sandbox.py`, `ai_earn/durable_sandbox_payments.py`, `docs/PAYMENTS.md`, `tests/test_payment_providers.py`
- **Task:** Keep sandbox HMAC as is; add `VerifiedSettlement` table + `reconcile(bank_csv)` drill (CSV import, no live bank). Require manual sign-off before `settled` → `verified`.
- **Acceptance:** `PaymentProviders.handle_webhook` sandbox → `settled` (HMAC) ≠ `verified`; `reconcile` matches `verified` only after CSV; same `event_id` idempotent still holds.

### P2-2 Connector OAuth real (drive/gmail/github)
- **Files:** `ai_earn/connectors/*`, `ai_earn/opportunities.py:Connector`, `tests/test_connectors*.py`
- **Task:** Implement `OAuthFlow` (`auth.py`) with PKCE, `DriveConnector.list` real HTTP behind `AI_EARN_CONNECTOR_LIVE=1` flag, otherwise stub. Add `revoke()` verification.
- **Acceptance:** With flag off → stub only; with flag + disposable OAuth test account → `list` returns real file list bounded 10k, `revoke` removes token.

### P2-3 Service proposal acceptance flow
- **Files:** `ai_earn/service_workspace.py:Proposals`, `ai_earn/crm.py`, `tests/test_service_workspace.py`
- **Task:** Add `proposal_accepted` event (customer → proposal) with `CustomerRequests` binding, audit, not external.
- **Acceptance:** `Proposals.create` draft → `CustomerRequests.create` → `Proposals.accept` with `operation_key` idempotent; cross-owner denied.

### P2-4 Tax & invoice PDF rendering
- **Files:** `ai_earn/invoices.py` (PDF stub SHA-256), `ai_earn/ledger.py`, new `ai_earn/invoice_pdf.py`
- **Task:** Render real PDF (stdlib `reportlab` requires dep → keep stub until dep approved; otherwise `pdf_stub_hash` remains). Add tax fields `tax_rate_paise`.
- **Acceptance:** If no dep, `hash` remains; with dep, `pdf_bytes` length >0 and `hash` matches.

---

## P3: AI & Automation

### P3-1 Persist provider config + metering
- **Files:** `ai_earn/ai_assistant.py:ProviderConfig`, `ai_earn/state_storage.py` (new `ai_provider_configs` table), `tests/test_ai_assistant.py`
- **Task:** Store `provider, model, base_url, api_key_hash` per owner (never raw key), `token_usage` per call.
- **Acceptance:** `AIAssistant.configure(provider="ollama", model="llama3")` persists, `health` shows `ai_available:true` when stub else false.

### P3-2 Consequential approval fence for real side-effects
- **Files:** `ai_earn/ai_assistant.py:ApprovalQueue`, `ai_earn/connectors/client.py`
- **Task:** Wrap `client.py` `send` with `ApprovalQueue.create` → `pending` → human `approve(confirmed=True)` → `execute`. TTL 10 min, preview.
- **Acceptance:** Call `ToolRegistry.get("send_email")` without approval → `ToolError` requires approval; with `approve` → executes (stub).

---

## P4: Frontend & UX

### P4-1 Design system + 17-view polish
- **Files:** `dashboard/app.html`, `app.js`, `app.css`, `connected.html`, `snapshot.js`
- **Task:** Add design tokens, responsive grid, empty/loading/error states per view, keyboard nav, `aria` labels.
- **Acceptance:** `tests/test_frontend_spa.py` (snapshot loader, empty states) still green; manual `http.server --bind 127.0.0.1` shows 17 views without overlap at 320px/1024px.

### P4-2 Live polling (opt-in)
- **Files:** `dashboard/snapshot.js`, `ai_earn/monitoring.py:Monitor`
- **Task:** `Snapshot.poll(interval=30s)` re-reads `dashboard-snapshot-*.json` if mtime changed, `POST /api/poll` stub when live.
- **Acceptance:** `Monitor.poll()` emits `health_check` every 30s; dashboard shows `Last updated Xs ago`.

### P4-3 Accessibility audit
- **Files:** `dashboard/*`, `docs/FRONTEND_STACK.md`
- **Task:** `axe-core` manual run, fix contrast/focus, add `skip link`.
- **Acceptance:** 0 critical `axe` violations on `connected.html`.

---

## P5: Production Hardening & Release

### P5-1 Postgres RLS design doc + PoC
- **Files:** `docs/DEPLOYMENT.md` §17, new `docs/POSTGRES_RLS.md`, `ai_earn/state_storage.py` (adapter)
- **Task:** Design per-user vault vs Postgres RLS; PoC `StateStorage` adapter that routes to `sqlite` local or `psycopg` (not bundled).
- **Acceptance:** Doc describes DDL `CREATE POLICY owner_isolation … USING (owner_id = current_setting('app.owner_id'))`; PoC tests skip unless `postgres` available.

### P5-2 TLS + secrets vault
- **Files:** `ai_earn/secrets.py`, `ai_earn/config.py`, `docs/SECRET_MANAGEMENT.md`, `.github/workflows/tests.yml`
- **Task:** Document `AI_EARN_*_DB` via `age` or OS keychain (`keyring` dep optional), reverse proxy `nginx` `127.0.0.1:8000` → `443` with `Secure;HttpOnly;SameSite` + CSP.
- **Acceptance:** `production_config.validate_production` fails if `AI_EARN_AUTH_DB` under `dashboard/`; `health` never serves DB.

### P5-3 Full `production_readiness` live enablement drill
- **Files:** `ai_earn/production_readiness.py`, `ai_earn/payment_providers.py:ProviderConfig`, `docs/PRODUCTION_READINESS.md`, `docs/PAYMENTS.md`
- **Task:** Dry-run 11 checks + `validate_production` on disposable prod-like env (`AI_EARN_ENV=production`, valid private DBs, `INFO` level, no `.env`, `127.0.0.1` only) → all PASS. Record `docs/PR40_COMPLETION_REPORT.md` style.
- **Acceptance:** `production_readiness.evaluate({AI_EARN_LIVE_APPROVAL:"signed"})` → `production_ready:true, live_ready:true, live_blocked:true` (still blocked until code lift); `ProviderConfig(provider="razorpay",mode="live",webhook_secret=b"x"*32)` still raises until lift.

### P5-4 Release gates
- **Files:** `.github/workflows/tests.yml` (add `pip-audit --require-hashes`, `ruff`, `mypy`, `windows_phase3_gate` mandatory), `docs/OPERATIONAL_RUNBOOKS.md`
- **Task:** 4-job matrix `ubuntu+windows × 3.11/3.13` must be green, `health --json --with-backups` + `doctor --json` artifacts uploaded, manual sign-off sheet in `docs/PRODUCTION_READINESS.md`.
- **Acceptance:** Tag `v0.2.0` only after sign-off + external pentest.

---

## Execution log (updated 2026-11-16)

- **P0-1 through P0-5:** completed in prior reviewed work on the active PR lineage (central readiness contract, fail-closed recovery/setup, backup/security hardening, stale-backup controls, display-name separation). See merged commits and the audit trail for exact implementation details.
- **P1-1:** completed. `ai_earn/tracker_cli.py` is a compatibility shim; the 1–82 tracker menu is split among focused modules under `ai_earn/tracker/`.
- **P1-2:** completed. Schema DDL/indexes and apply operations for upgrades v2–v28 are now in immutable `ai_earn/migrations/vNN.py` modules. The `StateStorage` validation/locking/backup boundary is unchanged; `_apply_vN` aliases remain for controlled fault-injection compatibility. Added `tests/test_migration_modules.py` and `docs/STATE_MIGRATIONS.md`. Full verification: 1,016 tests passed, 17 skipped; compileall, secret scan, and diff whitespace checks passed.
- **P1-3:** partially completed. Added `ai_earn/financial_math.py` as the shared exact-integer-paise net-cash calculation used by `finance`, `ledger`, `revenue_analytics`, and `dashboard_metrics`; invalid float/bool/negative components are rejected. `finance.py` is **not** deprecated into a `ledger.py` wrapper: it contains distinct v28 tax, billing, payment, expense, refund, fee, and reporting APIs, so redirecting it would break real functionality. The legacy API-wrapper portion and a broader reporting reconciliation remain open. Focused finance/dashboard/analytics suites pass after this change.
- **P1-4 onward / P2–P5:** still open; no claim of overall completion or production readiness is made.

## Effort & Dependencies

| Phase | Effort | Depends | Risk if skipped |
|---|---|---|---|
| P0 | 2–3 weeks, 1 Python eng + security review | none | Data loss, confusing readiness, backup plaintext |
| P1 | 3 weeks | P0 | Monolith blocks velocity, type drift |
| P2 | 4–6 weeks | P1 | No verified income, no real connectors |
| P3 | 2 weeks | P1 | AI side-effects without fence |
| P4 | 3 weeks | P1 | Poor adoption, a11y fail |
| P5 | 4 weeks + external | P0-P4 | No production legal/security claim |

---

## Verification Matrix (how to prove each P)

- `python -m compileall -q` + `ruff` + `mypy` + `tools/security_check` + `--audit` + `git diff --check`
- `python -m unittest discover -v` → 1014+ new tests, 0 fail, 17 skipped (Windows pending)
- Disposable lifecycle: `register → init (YES) → open → create opportunity/service/customer → ledger/invoice → snapshot → export → health --with-backups → doctor`
- Windows: `python -m unittest discover -v` on clean NTFS Windows → publish log, no `0.0.0.0`

---

*All tasks preserve APIs/DB migrations/CLI/dashboard contracts; no live payments, no real provider creds, no secrets in repo; small commits; do not merge until P5 sign-off.*
