# Security Review — Ai-earn (historical review 2026-11-17; current addendum 2026-11-22)

**Historical review baseline:** `arena/4bd4230e-ai-earn` (base `9d77101`, post-#37).  
**Baseline tests:** `1014 passed / 17 skipped` (Linux)  
**Current scope:** Local-first SQLite application with an authenticated loopback-only HTTP workspace API. No hosted public service is approved. This is a point-in-time engineering review, not an independent penetration test or security certification.

## Summary verdict

**NOT APPROVED for hosted production or live-money use.** The local disposable-data workflow has expanded since the historical review, but Windows operator acceptance, human accessibility review, restore drill, and independent security review remain outstanding. Preserve the local-only and live-payment restrictions.

## Current implementation addendum (2026-11-22)

This addendum supersedes historical statements below wherever the older review says that no HTTP server/API exists or describes the dashboard as snapshot-only. The original review notes are retained as historical evidence and were not re-run as an independent audit.

- **Local HTTP boundary:** `ai_earn/local_server.py` serves the SPA and `/api/v1` on `127.0.0.1:8765` only. It rejects non-loopback Host values, does not enable CORS, applies restrictive response headers, bounds request bodies, and is not approved for LAN, tunnel, reverse-proxy, or public exposure. Transport is plain HTTP on loopback; do not treat it as a remote-access interface.
- **Browser authentication:** the service token is stored in an HttpOnly, SameSite cookie and is not exposed to JavaScript. Mutating requests require same-origin Origin validation and a matching CSRF cookie/header pair. AuthService remains responsible for login throttling, expiry, and revocation.
- **Workspace API:** `ai_earn/workspace_api.py` delegates to existing owner-scoped domain services for opportunities, services, proposals, customers, products, tasks/projects, orders, invoices, payments, expenses, and ledger. Mutations use bounded input allowlists and idempotency keys; the HTTP layer does not directly mutate business tables. The live SPA uses this authenticated API; the snapshot path remains read-only fallback.
- **HTTP evidence:** PR #71 added HTTP-level end-to-end workspace coverage. PR #69 added payment safety coverage, including rejecting client-supplied pre-verification and preventing sandbox/provider-backed records from being locally marked verified. A manually recorded payment can be annotated verified only after it is settled; that is still an operator assertion and not independent bank reconciliation.
- **Database baseline:** current state schema is v30. Migrations v28–v30 add tax/billing/payment records, AI provider configuration and usage/settlement evidence, and a unique accepted-proposal-to-order link. See `docs/DATABASE.md`.
- **Remaining security gaps:** the local audit trail is not tamper-evident against coordinated database edits; auth and state stores do not commit atomically; local rate limiting is not a distributed network defense; secrets are environment-configured rather than backed by a managed vault; binary/private document upload is not exposed; actual Windows ACL behavior, real restore, and human accessibility have not been independently accepted.
- **Release decision:** local disposable-data use may be evaluated only after current CI and the Windows operator checklist pass. Hosted SaaS and live payments remain separate blocked gates. No independent reviewer sign-off is claimed here.

---

## Implemented controls (updated — now includes production hardening: 127.0.0.1-only, no .env, secrets redacted, structured logs allowlisted, SafeError 8-hex, backup verified 27 chain, monitoring poll, export 50MB redacted, deletion DELETE <8>, security_check, CI hardening; see docs/PRIVACY.md, SECRET_MANAGEMENT.md, OPERATIONAL_RUNBOOKS.md) 2026-11-17, production hardening added) (verified where noted)

- **Configuration:** `config.py` validates `AI_EARN_ENV`/`LOG_LEVEL`/`AUTH_DB`/`STATE_DB` without logging raw values; `load_database_paths` checks absolute paths before any file open. `ConfigurationError` leaks only variable name.
- **Auth storage:** `auth_storage.py` `create=True` vs `create=False` semantics (register may init, login/recover must not), `_check_parent` + `_ensure_file` (POSIX 0700/0600, Windows `verify_private_path`), `PRAGMA user_version` schema gate, `BEGIN IMMEDIATE` transactions, `mode=rw` URI prevents recreation race.
- **Password hashing:** `auth.py` `PASSWORD_ITERATIONS 600_000` PBKDF2-HMAC-SHA256 + 16-byte salt, `hmac.compare_digest` for hash & recovery hash, dummy derivation when account missing to avoid timing oracle, recovery hash is `sha256(token)` not plaintext.
- **Sessions:** 43-char urlsafe token (`secrets.token_urlsafe(32)`), stored as `token_hash`, 30-min expiry, revocation via `revoked_at`, `profile`/`logout`/`replace_recovery_code` re-check expiry after lock, separate `auth` then `state` lock order.
- **Rate limiting:** `auth_attempts` per-action per-subject + global window 15 min, `rate_limited` audit throttled, `RateLimitError` generic.
- **State storage:** `state_storage.py` distinct `APPLICATION_ID`, explicit backup-first migrations to new path, `validate_schema`/`check_integrity` (quick_check, foreign_key_check, creation-event, terminal-transition, multi-step replay, queue consistency, etc.), `_PrivateFiles` reuse, `closing` + `read-only immutable` opens for `doctor` and `backup_to`.
- **Authorization:** `state_authorization._authorized_transaction` derives actor from live session, checks `user_version` in (2..14), re-checks expiry before state commit. All domain services (`opportunity_drafts`…`durable_sandbox_payments`) use it and add `WHERE owner_id=?`.
- **Input validation:** Bounded titles 1..200 trimmed, amount paise 0..1e12, INR only, currency checks, bounded UTF-8 workflow output without control chars, HMAC amount/currency/timestamp/event-order checks in `durable_sandbox_payments`, duplicate/extra field rejection in `payment_sandbox`.
- **Idempotency:** `state_operations` `(owner_id, operation_key)` PK, `request_hash` (sha256 of canonical JSON), collision-checked generated child keys; exact retry returns original ID even after state change (caller must check `get_lease`).
- **Audit:** `state_audit` and `auth_audit` append-only (no update/delete in services), owner-scoped reads. Not tamper-evident.
- **Doctor:** `doctor.py` read-only `mode=ro&immutable=1`, parent privacy before file existence, file mode/ACL enforcement, `integrity_check`, no secret read.
- **Dashboard:** `dashboard_metrics.get` is read-only transaction, no token/username/title in snapshot, exclusive `O_EXCL|O_NOFOLLOW` create (POSIX) / `create_private_file` (Windows), `connected.html` loads file locally, `snapshot.js` 1-MB limit, no API.
- **Logging:** `events.py` allowlist `Event` enum only, no arbitrary message/exception; `write_event` respects `minimum_level`.

## Gaps & risks (what is NOT done)

| Area | Gap | Risk | Required fixing milestone |
|---|---|---|---|
| **Web / cookies / CSRF** | No HTTP server, no cookies, no CSRF | N/A now, but any future web must add `Secure`, `HttpOnly`, `SameSite`, CSRF tokens | M14 |
| **File uploads** | No upload handling | Traversal/execution/resource exhaustion | M6/M14 |
| **Auth/state atomicity** | Separate commits, ambiguous on crash after state commit | Stale lease or duplicate external effect if retried naïvely | M2 – document, add recovery comparison before replay |
| **Audit tamper-evidence** | `DELETE FROM state_audit` + coordinated `state_operations` deletion can hide marker (rehearsal demonstrates) | Coordinated DB edit evades checks | M2 – accept limitation or add hash chain (out-of-scope for local MVP) |
| **Legacy association bypass** | Unadopted legacy `task_associations` remain independently mutable (`TaskQueue`/`TaskWorkflows` direct) | Bypass of paired lifecycle | M2 – migrate or document; managed opt-in already denies direct for fresh |
| **Stale snapshot replay** | Restored old copy lacks later `operation_key`, can create second lease | Duplicate work if old copy replayed | M2 – never auto-replay, `compare_snapshot` is accountability only |
| **Payment verification** | `payment_observations` + synthetic sandbox are not provider verification | Treating screenshot as proof | M9 – real Razorpay webhook HMAC + reconciliation |
| **Secrets** | No vault, env-only, `.env` not loaded | Accidental commit | M15 – secret manager, `.env.example` redacted (already), CI secret scan |
| **Dependencies** | No lockfile (stdlib only) | Supply-chain risk when deps added | M2 – review, pin, lock |
| **Windows** | ACL checks rely on `Win32` + fixed drive + persistent ACLs; `nt` fallback disabled if `_windows_auth_verified()` false | Unsafe dir/file if check skipped | M2 – gate already fails closed, user-reported `72241cc`/`f26c6b9` skips accepted |
| **Rate limit is local** | Not a network defense | Brute force if process hostile | Accepted for local MVP; future server needs Redis/DB-backed limit |

## Threat model (condensed)

- **Assumptions:** Local OS account trusted, private dir not shared, `AI_EARN_*_DB` absolute paths, process not compromised. Anyone with DB read/write can bypass local authorization – not an OS sandbox.
- **Attackers:** Local other OS user with file access, malicious process, compromised backup, network attacker (if future web exposed).
- **Mitigations in place:** Parent/file privacy gates fail closed, absolute-path check, symlink rejection, sidecar existence check before create, operation-key child collision check, foreign keys, checked `allow_admin_owner` only for sidecar inside verified dir.

## Test coverage of security properties (this baseline)

- Unauthorized & cross-account denial: `test_auth`, `test_state_authorization`, `test_task_associations`, `test_task_queue`, etc. – all include `wrong_owner`/`invalid_session` cases.
- Expired/revoked session: `test_auth` expiry, `test_windows_auth` session expiry after lock.
- Permission escalation: `test_state_authorization` same-DB hard-link refusal (POSIX-only).
- Injection: bounded titles (trim), `length(title)` + `GLOB` checks in schema, parameterized queries everywhere, JSON field strictness in `durable_sandbox_payments`.
- Leakage: `test_state_snapshot_integrity`, `test_durable_sandbox_payments` `no_plaintext_*` tests (output hash not plaintext), `doctor` never prints path value, `auth` dummy derivation, `dashboard` no token.
- Failing closed: `test_windows_security` 4 tests for ACL policy, `test_state_operation_integrity` for removed audit rows, etc.

All above passed in 636/17 baseline.

## Recommendations (prioritized)

1. **M2:** Complete Phase 3 recovery drills (temp DBs, `os._exit` before/after commit, thread+process races) and document `BACKUP_AND_RECOVERY.md`; keep `production_ready:false`.
2. **M2:** Add `docs/DATABASE.md` with ER diagram, state machines, transaction boundaries, and `doctor` usage.
3. **Before any web:** Design session cookies (`Secure; HttpOnly; SameSite=Lax; __Host-`), CSRF, rate limit backed by DB, and per-record authorization tests (blocked by M14).
4. **Before any live money:** Razorpay webhook signature + reconciliation + `compare_snapshot` for ledger, checklist in `PAYMENTS.md` (blocked until M9/M11).

## Change log of this review

- 2026-11-16: Initial `SECURITY_REVIEW.md` created from `SECURITY.md` + `THREAT_MODEL.md` + audit of `auth.py`, `state_storage.py`, `doctor.py`, `windows_security.py`, and 636-test baseline. Replaces informal “security implications” scattered in `PHASE_STATUS.md` but does not delete `SECURITY.md` (history preserved).
