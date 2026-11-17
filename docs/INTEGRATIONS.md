# Integrations — Ai-earn (2026-11-17 Phase 12+ (v28))

**Status:** Reusable connector framework implemented (963 tests) (v28, sandbox stubs). No live network, no live OAuth. Manual workflows preserved.

## Reusable connector framework (implemented)

```
ai_earn/connectors/
  base.py          – Connector ABC + 10 capabilities (scopes, status, permission, logging, error normalization)
  auth.py          – validate_api_key 32..128, redact_config
  scopes.py        – LEAST_PRIVILEGE_SCOPES, validate_scopes_for (least-privilege)
  throttle.py      – Throttle per-owner per-connector (limit 1..100, window 1..3600, thread-safe)
  retry.py         – RetryPolicy max_retries 0..5, exponential backoff, should_retry (no retry on auth/invalid/rate-limit)
  errors.py        – normalize_error, is_transient (maps HTTP → ConnectorError without leaking secrets)
  normalize.py     – normalize_gmail/drive/github (+ calendar/marketplace) bounded 1..200 title, 1..2048 URL, 0..1e12 budget
  status.py        – ConnectionState, check_connection_status (never exposes tokens)
  client.py        – BaseAPIClient (throttling + permission + retry + error normalization + redacted logging)
  registry.py      – available_connectors, create_connector, connection_status_all, manual_fallback_message
  gmail.py         – GmailConnector  (gmail.readonly)
  drive.py         – DriveConnector  (drive.file)
  github.py        – GitHubConnector (repo.read)
  calendar.py      – CalendarConnector (calendar.readonly)
  marketplace.py   – MarketplaceConnector (upwork/fiverr, opportunity.read, permitted methods only, never ToS/robots/CAPTCHA bypass)
  payment.py       – PaymentConnector (Razorpay/Stripe sandbox, payment.orders.read)
```

**Each connector provides 10 capabilities:**

1. **Authentication handling** – API key/token via `validate_api_key`, stored as bytes, never logged.
2. **Credential validation** – bounded 32..128, no control/space, `validate_config()` without network, scopes validated.
3. **API client** – `BaseAPIClient.request()` wraps `fetch` with throttling, permission, retry, error normalization, redacted logging.
4. **Request throttling** – `Throttle` fixed-window, per-owner per-connector, `check()` + `reset()`, tested isolation.
5. **Error normalization** – `normalize_error()` maps 429/ auth/ permission/ invalid to `ConnectorError`/`AuthError`/`ThrottleError`/`PermissionError` without secret leakage.
6. **Retry handling** – `RetryPolicy` exponential `delay_for()` capped, `should_retry()` only transient (not auth/invalid/rate-limit), integrated in `BaseAPIClient`.
7. **Data normalization** – `normalize_*` rejects extra fields, validates bounded title/url/budget/deadline/risk/tags, returns `title/source_url/budget_paise/deadline_at/risk_level/skill_tags`.
8. **Logging** – allowlist events (`fetch/validate/throttle/retry/error/status/request`), `redact_config()` replaces `api_key/token/secret` with `***redacted***`, never logs raw tokens, `BaseAPIClient.logs()` returns copies.
9. **Connection status** – `connection_status()` / `check_connection_status()` returns `{connector_id,status,connected,revoked,scopes}` never credentials; supports `connected/revoked/error/manual`.
10. **Permission checks** – `check_permission(owner_id)` validates 32-hex owner, connector_id, revoked, least-privilege scopes, owner-isolated.

All integrations **optional** via `registry.create_connector()` and `available_connectors()`; failures preserve **manual workflow** (`ManualConnector` in `opportunities.py`, `tracker_cli` manual title+INR). If a connector is revoked or unavailable, `opportunities.import_from_connector` raises `ConnectorError`/`OpportunityError` and caller falls back to manual import (zero results, no fabricated data).

**Least-privilege scopes:**

| Connector | Least scope(s) | Notes |
|---|---|---|
| Gmail | `gmail.readonly` | stub, 2 items, no `gmail.send` until explicit approval |
| Drive | `drive.file` | stub; `drive.readonly` not requested |
| GitHub | `repo.read` | read-only issues |
| Calendar | `calendar.readonly` | stub, 2 events |
| Marketplace (Upwork/Fiverr) | `opportunity.read` | stub, permitted methods only, respects ToS/robots, manual import remains |
| Payment (Razorpay/Stripe) | `payment.orders.read` | sandbox only, wraps `payment_providers` sandbox, no card storage |

## Candidate integrations (optional, all sandbox stubs until credentials & review)

| Integration | Purpose | Scopes (least) | Stub | Fallback | Blocker until |
|---|---|---|---|---|---|
| Gmail | Lead/customer comms | `gmail.readonly` | `GmailConnector` 2 items | Copy-paste manual | OAuth consent, user approval |
| Google Drive | Portfolio/product files | `drive.file` | `DriveConnector` 2 items | Local file hash/size only | OAuth |
| GitHub | Portfolio source | `repo.read` | `GitHubConnector` 2 issues | Manual link | PAT |
| Calendar | Deadlines/follow-ups | `calendar.readonly` | `CalendarConnector` 2 events | Manual date | OAuth |
| Upwork/Fiverr | Opportunity discovery | `opportunity.read` | `MarketplaceConnector` 2 gigs, platform flag | Manual import (M3) | API eligibility, ToS, permitted method |
| Razorpay/Stripe | Payments | `payment.orders.read` | `PaymentConnector` 1 order | Synthetic sandbox | Merchant KYC, `PRODUCTION_READINESS.md` |
| Other approved tools | Extensible via `BaseAPIClient` + `registry` | per-tool least | Add `ai_earn/connectors/<tool>.py` following same 10 | Manual | Tool-specific review |

If an integration is unavailable, preserve manual workflow – never invent listings, never bypass CAPTCHA, never abuse accounts (per `LEGAL-001`).

## Security notes

- Credentials via `AI_EARN_*_TOKEN` env, 32..128 bytes/str, validated without control/space, stored as bytes, never committed, never returned in `dashboard_metrics` or connector `fetch` results, never in logs (`redact_config`, `BaseAPIClient._log_event` rejects credential substrings, `normalize_error` redacts).
- TLS validation, timeout, revocation tests required per connector (see `SECURITY_REVIEW.md`); stubs have `revoke()` + `connection_status()` tests.
- Request caps: `Throttle` 10/min default per owner+connector; `RetryPolicy` capped 5 retries, exponential backoff, no retry on auth/invalid/rate-limit (tests cover per-owner isolation, reset, transient vs auth).
- Connection status visible via `registry.connection_status_all()` and future `Integrations` UI, with “Reconnect” + “Revoke”.

Until live OAuth is approved, stubs remain deterministic, bounded (limit 1..100, 100 cap), no network, no ToS bypass. Do not add third-party SDK dependencies without review.
