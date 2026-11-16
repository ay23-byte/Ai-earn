# API — Ai-earn (2026-11-16)

**Status:** No HTTP API exists. This document records the current CLI/service boundaries and the planned API contract so future implementation is consistent.

## Current boundaries (CLI, not HTTP)

- **Entry:** `ai_earn/__main__.py` – dispatches `python -m ai_earn status | auth {register|login|recover} | tracker {init|open} | dashboard snapshot | doctor [--plan-init|--json]`. No network listener.
- **Auth:** `AuthService.register(username, password) → recovery_code`, `login(username, password) → token`, `profile(token) → {id, username}`, `logout(token)`, `replace_recovery_code(token, confirmed=True) → new_code`, `recover(username, recovery_code, new_password) → new_code`. All via `AuthStorage` transaction, rate-limited, `AccessDenied` on bad token.
- **State:** All domain services expose Python methods with `(token, …, operation_key, confirmed=True)` signatures, internally via `state_authorization._authorized_transaction`. No direct `StateStorage` use is allowed for untrusted callers.
- **Responses:** `responses.success(data)` / `failure(code, message)` envelope `{ok, data, error}` used only by `status` CLI. Errors never leak values.
- **Events:** `events.Event` allowlist (`STATUS_REQUESTED`, `INVALID_REQUEST`, `INVALID_CONFIGURATION`, `INTERNAL_ERROR`, `DOCTOR_REQUESTED`, `DOCTOR_PLAN_REQUESTED`), `write_event` to stderr.

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
