# API — Ai-earn (2026-11-17, LATEST 28, branch arena/a2b30614-ai-earn)

**Status:** No HTTP API exists (2026-11-17, branch arena/a2b30614-ai-earn, LATEST 28, 963 tests). This document records the actual CLI/service boundaries tested via `tests/test_api_contract.py` (14) + `tests/test_e2e_journey.py` (2) and the planned HTTP API contract so future implementation is consistent. `health`/`status`/`auth`/`dashboard snapshot` are actual; HTTP remains planned.

## Current boundaries (CLI, not HTTP)

- **Entry:** `ai_earn/__main__.py` – dispatches `python -m ai_earn status | auth {register|login|recover} | tracker {init|open} | dashboard snapshot | doctor [--plan-init|--json] | health [--json] [--with-backups] | data {export|delete} | production`. No network listener (all `127.0.0.1` only if HTTP added, never `0.0.0.0` with SQLite; see `docs/DEPLOYMENT.md`). Tested via `test_api_contract` (API/ auth/ isolation/ health contracts) and `test_e2e_journey`.
- **Auth:** `AuthService.register(username, password) → recovery_code`, `login(username, password) → token`, `profile(token) → {id, username}`, `logout(token)`, `replace_recovery_code(token, confirmed=True) → new_code`, `recover(username, recovery_code, new_password) → new_code`, `account deletion` (`account_deletion.py` `prepare_deletion_backup` → `confirm_deletion` with `DELETE <8>` + password re-entry, owner-only, backup verified). All via `AuthStorage` transaction, rate-limited, `AccessDenied` on bad/expired/revoked token (tested failure conditions in `test_auth`, `test_api_contract`, `test_e2e_journey`).
- **State:** All domain services (opportunities, services, products, customers, communications, ledger, followups, invoices, provider orders, AI approvals, etc. v1→v28) expose Python methods with `(token, …, operation_key, confirmed=True)` signatures, internally via `state_authorization._authorized_transaction` (`WHERE owner_id=?`, recheck expiry before commit). No direct `StateStorage` use is allowed for untrusted callers. Tested via `test_api_contract` (integration, authorization, isolation) + `test_e2e_journey` (full journey), plus per-service unit tests (28 meaningfully with `assertRaises` for `AccessDenied`/`ProviderError` etc.).
- **Responses:** `responses.success(data)` / `failure(code, message)` envelope `{ok, data, error}` used by `status`/`health` CLI (`health` JSON `{ok, production_ready:false, checks:{config,auth,state,version,backups}, version}`, exit 0/1/2, no secrets; see `test_health` 9 + `test_api_contract` health contract). Errors never leak values (`error_reporting.SafeError` 8-hex, redacted logs; tested in `test_phase16_production_hardening`).
- **Events:** `events.Event` allowlist (`STATUS_REQUESTED`, `INVALID_REQUEST`, `INVALID_CONFIGURATION`, `INTERNAL_ERROR`, `DOCTOR_REQUESTED`, `DOCTOR_PLAN_REQUESTED`, `HEALTH_REQUESTED`, `BACKUP_*`, `EXPORT_*`, `DELETION_*`), `write_event` to stderr (level-filtered, never arbitrary). Structured logs via `structured_logging.StructuredLogger` (allowlist JSON, redacted `***`, never DB paths; tested in `test_phase16_production_hardening` + `test_api_contract` unit).

## Planned HTTP API (not implemented, design only)

When a web tier is introduced (M14, after M2), it must follow:

- **Auth:** Session cookies `__Host-session` (`Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=1800`), no token in JS, CSRF token per form, `Authorization: Bearer` not used for browser. `X-Content-Type-Options: nosniff`, `CSP` without `unsafe-inline`.
- **Authorization:** Every handler must call `state_authorization` equivalent and filter `WHERE owner_id=?`. No direct `state_storage` from handler.
- **Validation:** JSON schema per route, reject unknown fields, bounded strings (1..200), amount paise integer, enum checks. Normalize `trim(title)`.
- **Idempotency:** Client-supplied `Idempotency-Key` header mapped to `state_operations(operation_key)`, same semantics as CLI `operation_key`.
- **Response schema:** `{ok: bool, data?: object, error?: {code: string, message: string}}`, `traceId` header, structured logs with `traceId` not secrets.
- **Routes (draft):** `POST /auth/register`, `/auth/login`, `/auth/logout`, `/auth/recover`, `GET /me`, `POST /opportunities`, `PATCH /opportunities/{id}/archive`, `POST /task-plans`, etc., mirroring current service methods. Versioned under `/api/v1`.
- **Dashboard:** Snapshot remains read-only; future live dashboard must poll `GET /dashboard/metrics` (owner-scoped) with same `rupees/count` semantics.

No route is implemented; this file exists to prevent ad-hoc divergence. Implementation requires M2 database completion and M14 frontend decision (ADR-006).
