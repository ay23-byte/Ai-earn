# Payments — Ai-earn (2026-11-16)

**Status:** Sandbox provider HMAC verified (v21), **no live transactions**, **no card storage**. Razorpay/Stripe shaped, sandbox only until explicit approval.

## Current synthetic implementation (experimental)

- **`ai_earn/payment_sandbox.py`** – in-memory `SandboxPaymentEvents`, HMAC of exact body, bounded JSON, sequence/transition checks, `sandbox_*` stages, lock-serialized, lost on restart. Not connected to DB, not a webhook.
- **`ai_earn/durable_sandbox_payments.py`** – SQLite `sandbox_payments` (fingerprint, amount, `sandbox_*` stage/sequence) + `sandbox_payment_events` (event_id, type, body_hash), `accept(token, payment_id, body, signature, operation_key)` inside `auth`→`state` transaction, rechecks freshness/sequence/HMAC before commit, exact retry returns historical ack. Stores `key_fingerprint = sha256(test_key)`, not raw key. `compare_snapshot` compares recognised rows only.
- **`ai_earn/payment_providers.py` v21** – SQLite `payment_provider_orders` (provider razorpay/stripe, mode sandbox/live, amount INR, status `created→authorized→captured→settled→refunded/failed`, provider_order_id 1..64, owner-scoped) + `payment_provider_events` (event_id 1..64, order_id FK, raw_body_hash BLOB 32), `ProviderConfig` (webhook_secret 32..128, live blocked), `create_order` + `handle_webhook` (raw-body HMAC **before JSON parse**: Razorpay `HMAC-SHA256(body, secret)` hex, Stripe `HMAC-SHA256(t.body, secret)` with `t=` freshness 300s, bounded JSON `event_id/event_type/provider_order_id/amount/currency/occurred_at` no extra fields, amount binding, transition checks `created→authorized→captured→settled→refunded/failed`, idempotency via `operation_key` + `event_id` event hash, duplicate event_id with different body or different operation_key fails closed, `get`/`list` owner-scoped, `reconcile` local-only), `dashboard_metrics` `provider_orders/events` `sandbox_only`, `state_recovery_comparison` `compare_snapshot` `provider_events`, `tracker 31-33` (create/list/webhook).
- **Labels:** `payment_observations` are `unverified` placeholders (order→invoice snapshot), `financial_assertions` are `unverified` stages, `sandbox_*`/`provider_*` never revenue unless provider `settled` verified. Dashboard shows `Verified income: Not available` (no live ledger).

## Ledger concepts (separate, never inferred)

1. Proposed (order draft)
2. Quoted (order draft)
3. Contracted (financial_assertion `contract_asserted` – unverified)
4. Invoiced (invoice draft, not issued) + `invoices` v20 `draft→issued→paid/overdue/void` PDF stub
5. Payment initiated (`initiated_asserted` – unverified)
6. **Payment received (verified) – NOT IMPLEMENTED** (`provider authorized/captured` are sandbox HMAC, not verified receipt)
7. **Payment settled (provider) – SANDBOX ONLY** (`payment_provider_orders` `settled` is HMAC sandbox, not bank settlement)
8. Refunds – SANDBOX ONLY (`refunded` via webhook `refunded` idempotent, not bank refund)
9. Expenses – `ledger_entries` `expense` unverified
10. Fees – `ledger_entries` `fee` unverified
11. **Net realized = settled − refunds − fees − costs – SANDBOX ARITHMETIC** (`ledger_11.net_realized_synthetic` is synthetic, not verified)

Only **6 & 7** after independent **live** provider verification (M11 gate) may contribute to verified income; v21 sandbox is local HMAC only.

## Future provider (Razorpay candidate, per `DECISIONS.md`)

- **Selected but not verified:** Eligibility, KYC, sandbox access, credentials all `NOT STARTED` (blocked, M9 live).
- **Implemented sandbox (M9 v21):** `PaymentProviders` `ProviderConfig` `provider=razorpay|stripe` `mode=sandbox` `webhook_secret` 32..128, `create_order` local, `handle_webhook` raw-body HMAC before JSON parse (Razorpay hex, Stripe `t=,v1=`), reject extra/duplicate fields, amount/currency strictly equals `payment_provider_orders`, idempotency via `operation_key` + `event_id` `raw_body_hash`, replay 300s, transition `created→authorized→captured→settled→refunded/failed`, `compare_snapshot` `provider_events`, `dashboard` `provider_orders` `sandbox_only`, `reconcile` local-only.
- **Required before any live (M11 gate):** Real Razorpay/Stripe webhook secret verified against live provider, `live` mode enablement after explicit approval, settlement polling, bank reconciliation, `PAYMENTS.md` checklist signed, `live` transactions still blocked.

## Gate (M11)

No live mode without: eligibility, HMAC + replay tests, duplicate/ordering tests, refund/failed/settlement tests, audit + ledger tests, backup/recovery tests, config separation review, explicit approval. Live transactions remain unauthorized.

Preserve warning: “Never mark a payment as received based only on a client-side success screen or unverified placeholder.”
