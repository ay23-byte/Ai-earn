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
- This initial API exposes read-only dashboard metrics plus login/logout/profile. Domain write endpoints and record-level CRUD screens are not yet exposed. The existing JSON snapshot loader/export remains available as an offline fallback.