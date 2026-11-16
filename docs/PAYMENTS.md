# Payments — Ai-earn (2026-11-16)

**Status:** No real payments. Synthetic only, **no provider integration**, **no live transactions**. This document is the gate for future Razorpay/Stripe work.

## Current synthetic implementation (experimental)

- **`ai_earn/payment_sandbox.py`** – in-memory `SandboxPaymentEvents`, HMAC of exact body, bounded JSON, sequence/transition checks, `sandbox_*` stages, lock-serialized, lost on restart. Not connected to DB, not a webhook.
- **`ai_earn/durable_sandbox_payments.py`** – SQLite `sandbox_payments` (fingerprint, amount, `sandbox_*` stage/sequence) + `sandbox_payment_events` (event_id, type, body_hash), `accept(token, payment_id, body, signature, operation_key)` inside `auth`→`state` transaction, rechecks freshness/sequence/HMAC before commit, exact retry returns historical ack. Stores `key_fingerprint = sha256(test_key)`, not raw key. `compare_snapshot` compares recognised rows only.
- **Labels:** `payment_observations` are `unverified` placeholders (order→invoice snapshot), `financial_assertions` are `unverified` stages, `sandbox_*` never revenue. Dashboard shows `Verified income: Not available`.

## Ledger concepts (separate, never inferred)

1. Proposed (order draft)
2. Quoted (order draft)
3. Contracted (financial_assertion `contract_asserted` – unverified)
4. Invoiced (invoice draft, not issued)
5. Payment initiated (`initiated_asserted` – unverified)
6. **Payment received (verified) – NOT IMPLEMENTED**
7. **Payment settled (provider) – NOT IMPLEMENTED**
8. Refunds – NOT IMPLEMENTED
9. Expenses – NOT IMPLEMENTED
10. Fees – NOT IMPLEMENTED
11. **Net realized = settled − refunds − fees − costs – NOT IMPLEMENTED**

Only **6 & 7** after independent provider verification may contribute to verified income.

## Future provider (Razorpay candidate, per `DECISIONS.md`)

- **Selected but not verified:** Eligibility, KYC, sandbox access, credentials all `NOT STARTED` (blocked, M9).
- **Required before any live:** Documented raw-body HMAC verification (Razorpay `X-Razorpay-Signature` = `HMAC-SHA256(body, webhook_secret)` over raw bytes before JSON parse, reject extra/duplicate fields, order amount/currency strictly equals our `order_drafts`, event `order` association, idempotency via `event_id`, replay protection, `compare_snapshot` reconciliation, refund/settlement/failed handling, `PAYMENTS.md` checklist signed, user explicit approval.
- **Sandbox plan (M9):** Modular `PaymentProvider` interface (`create_order`, `handle_webhook`, `reconcile`), separate `sandbox` vs `live` config, no card storage, no client-side success as proof, webhook timeout/retries, settlement polling.

## Gate (M11)

No live mode without: eligibility, HMAC + replay tests, duplicate/ordering tests, refund/failed/settlement tests, audit + ledger tests, backup/recovery tests, config separation review, explicit approval. Live transactions remain unauthorized.

Preserve warning: “Never mark a payment as received based only on a client-side success screen or unverified placeholder.”
