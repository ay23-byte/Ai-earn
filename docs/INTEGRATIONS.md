# Integrations — Ai-earn (2026-11-16)

**Status:** No live connectors. Manual workflows exist. This document is the framework spec.

## Connector framework (planned, M11)

```
ai_earn/connectors/__init__.py  – Connector ABC
  - authenticate(credentials) → token
  - validate() → bool
  - client() → API client
  - throttle() / retry() / normalize()
  - log(event) – allowlist only
  - status() → {connected, scopes, expires_at}
  - revoke() – least privilege
```

- Every connector: narrow scopes, `least_privilege`, request caps (429 handling, exponential backoff), error normalization, credential storage via env/secret manager, never in logs/frontend, manual fallback.

## Candidate integrations (all BLOCKED until credentials & review)

| Integration | Purpose | Scopes (least) | Fallback | Blocker |
|---|---|---|---|---|
| Gmail | Lead/customer comms | `gmail.send` only after explicit approval | Copy-paste manual | OAuth consent, user approval |
| Google Drive | Portfolio/product files | `drive.file` | Local file upload (future) | OAuth |
| GitHub | Portfolio source | `repo` read | Manual link | PAT |
| Calendar | Deadlines/follow-ups | `calendar.readonly` | Manual date | OAuth |
| Upwork/Fiverr | Opportunity discovery | Official API where permitted | Manual import (M3) | API eligibility, ToS, robots |
| Razorpay/Stripe | Payments | `orders`, `webhooks` | Synthetic sandbox | Merchant KYC (M9) |

If an integration is unavailable, preserve manual workflow – never invent listings, never bypass CAPTCHA, never abuse accounts (per `LEGAL-001`).

## Manual fallback currently (what works without credentials)

- **Opportunities:** `tracker_cli` manual title + INR amount, or `opportunity_drafts` service; no auto-discovery.
- **Customers/orders/invoices:** `order_drafts` + `invoice_drafts` + `financial_assertions` (unverified); no CRM sync.
- **Files:** `task_workflows` hash/size only; no Drive.
- **Payments:** synthetic `durable_sandbox_payments` only.

## Security notes

- Credentials via `AI_EARN_*_TOKEN` env, never committed, never returned in `dashboard_metrics`.
- TLS validation, timeout, revocation tests required per connector (see `SECURITY_REVIEW.md`).
- Connection status visible in future `Integrations` UI, with “Reconnect” + “Revoke” and audit.

Until `ai_earn/connectors/` exists, do not add third-party SDK dependencies.
