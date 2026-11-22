# Local HTTP API — Earn OS

**Status:** Implemented loopback-only local API for existing authenticated services. This is not a hosted or production deployment interface.

## Start

Set AI_EARN_AUTH_DB and AI_EARN_STATE_DB to the existing absolute database paths in the same private directory, then run:

```powershell
python -m ai_earn serve
```

The server binds only to 127.0.0.1:8765 and serves the SPA and /api/v1 from the same origin. It opens existing databases only: it does not create accounts, initialize stores, or run migrations. Create an account using the existing interactive auth register command first. Ctrl+C stops the server.

## Routes

- GET /api/v1/csrf — issue a same-site CSRF token cookie and return its value to the same-origin UI.
- POST /api/v1/auth/login — authenticate through AuthService.login; the service token is placed only in an HttpOnly, SameSite=Strict cookie.
- POST /api/v1/auth/logout — revoke the session through AuthService.logout and clear the cookie.
- GET /api/v1/me — validate the cookie-backed session and return the signed-in account profile.
- GET /api/v1/dashboard/metrics — validate the session and call DashboardMetrics.get; returns the same snapshot-compatible metrics shape used by offline exports.

All POST requests require a same-origin Origin header and matching CSRF cookie/header. Request bodies are bounded, unknown login fields are rejected, and API responses use {ok,data,error}. JavaScript never receives the authentication token. The server rejects non-loopback Host headers, has no CORS support, and sets no-store, nosniff, frame-denial and restrictive CSP headers.

## Security boundaries and limitations

- Domain reads continue through DashboardMetrics and _authorized_transaction; the HTTP layer does not query business tables directly.
- AuthService retains password hashing, login throttling, expiry and revocation.
- Existing owner-scoped metrics and financial verification semantics are unchanged.
- The server is intentionally local-machine oriented and must not be exposed through a reverse proxy, LAN bind, tunnel, or public internet. It is plain HTTP on loopback; do not use it for remote access.
- The dashboard metrics endpoint remains read-only and snapshot-compatible. Authenticated workspace and AI endpoints are documented below. The existing JSON snapshot loader/export remains available as an offline fallback.

## Authenticated local workspace API (v1)

All workspace routes are served only by the local loopback server started with the Earn OS CLI. They require the HttpOnly authenticated session cookie. Mutating requests require the same-origin Origin, the CSRF cookie/header pair, and a JSON object. Supply an Idempotency-Key header for safe retries; the server generates one when omitted. All domain authorization, validation, status transitions and audit writes are delegated to the existing domain service.

- GET /api/v1/workspace/{resource}?limit=20&after_id=...&status=...
- GET /api/v1/workspace/{resource}/{id}
- POST /api/v1/workspace/{resource}
- PATCH /api/v1/workspace/services/{id} (service content only)
- POST /api/v1/workspace/{resource}/{id}/actions/{action}
- GET /api/v1/workspace/ledger/summary

Initial resource adapters: opportunities, services, proposals, customers, products, tasks, customer-linked projects, order drafts (create/get only), invoices, payments, expenses and ledger entries. Client notes and requirements are exposed as owner-scoped customer subresources. Supported transitions are intentionally allowlisted in the adapter and enforced again by each domain service. Financial amounts are integer paise. Verified status is never inferred from payment status.

The current domain model does not expose generic deletion for these resources, so the HTTP API does not invent delete endpoints. Project records are represented by existing project_tasks linked to customers through customer_projects; the API creates the task and relationship using deterministic idempotency keys. Binary file uploads and private document storage are not yet exposed; existing attachment tables are metadata-only. Connector account management and non-AI integration settings remain outside this API surface. The AI provider settings described below are the limited local exception.

## AI assistant API

All AI routes require the authenticated session cookie and same-origin CSRF protection for POST requests.

- GET `/api/v1/ai/status` — provider availability/config summary, approval count, tool registry, emergency-stop state, and owner-scoped token-usage totals.
- POST `/api/v1/ai/provider` — configure only `stub` or `ollama` with `provider`, `model`, and optional `base_url`. The Ollama endpoint must be plain HTTP on a numeric loopback address (default `http://127.0.0.1:11434`). Remote providers and API-key fields are rejected by this route.
- POST `/api/v1/ai/chat` — generate an unverified draft. Optional reference content is passed as untrusted data. Ollama usage counters are persisted when the local server reports them.
- POST `/api/v1/ai/proposals` — prepare a tool preview; consequential tools create an approval request rather than executing.
- GET `/api/v1/ai/approvals`, GET approval detail/preview, POST approval decision/revoke, and POST emergency stop/clear — owner-scoped approval controls. Explicit confirmation is required for decisions and emergency controls.

The Ollama adapter calls only the local `/api/tags` and `/api/chat` endpoints, uses bounded JSON responses and configured timeouts, and never streams. Local model output is always `draft_unverified`; it cannot send messages, publish services, modify finance records, or execute consequential tools by itself. OpenAI-compatible and hosted network providers remain disabled.
