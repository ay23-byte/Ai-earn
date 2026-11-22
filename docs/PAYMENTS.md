# Payments — Ai-earn (2026-11-22; local payment boundary)

**Status:** Local finance records and sandbox provider HMAC workflows are implemented; **live provider transactions remain blocked** and no card data is stored. Razorpay/Stripe configurations are sandbox candidates, not verified merchant integrations. PR #69 adds server-side payment verification guardrails.

## Current finance and verification behavior (after PR #69)

- The live local workspace API uses `PaymentRecords` and `ExpenseRecords` through `ai_earn/finance.py`; records persist in the private state database and are owner-scoped.
- Payment creation does not accept a client-supplied `verified` flag. The workspace API rejects attempts to create a pre-verified record.
- A payment may receive an explicit verification annotation only when it is a **manual** record and its status is already **settled**. Provider-backed and sandbox records cannot be promoted to verified by the local UI/API.
- Verification is an auditable operator assertion. It does not independently prove a bank deposit, provider settlement, or legal/accounting correctness. The operator must check external evidence outside Ai-earn.
- Expense creation, listing, persistence, and owner isolation are covered by the PR #69 finance workflow tests. The dashboard uses linked invoice/order selectors and displays provider/verification context.
- PR #69 CI passed all four Python 3.11/3.13 Ubuntu/Windows matrix jobs. The current main/PR CI status is recorded in `docs/CURRENT_RELEASE_CHECKPOINT.md`; do not reuse historical test totals as current evidence.
- **Live boundary unchanged:** no live provider credentials, KYC, production webhook, bank reconciliation, or live transaction is approved. `ProviderConfig(mode="live")` remains blocked pending the full M11 gate and explicit human approval.

---

## Provider sandbox implementation (v21; experimental)

- **`ai_earn/payment_sandbox.py`** – in-memory `SandboxPaymentEvents`, HMAC of exact body, bounded JSON, sequence/transition checks, `sandbox_*` stages, lock-serialized, lost on restart. Not connected to DB, not a webhook.
- **`ai_earn/durable_sandbox_payments.py`** – SQLite `sandbox_payments` (fingerprint, amount, `sandbox_*` stage/sequence) + `sandbox_payment_events` (event_id, type, body_hash), `accept(token, payment_id, body, signature, operation_key)` inside `auth`→`state` transaction, rechecks freshness/sequence/HMAC before commit, exact retry returns historical ack. Stores `key_fingerprint = sha256(test_key)`, not raw key. `compare_snapshot` compares recognised rows only.
- **`ai_earn/payment_providers.py` v21** – SQLite `payment_provider_orders` (provider razorpay/stripe, mode sandbox/live, amount INR, status `created→authorized→captured→settled→refunded/failed`, provider_order_id 1..64, owner-scoped) + `payment_provider_events` (event_id 1..64, order_id FK, raw_body_hash BLOB 32), `ProviderConfig` (webhook_secret 32..128, live blocked), `create_order` + `handle_webhook` (raw-body HMAC **before JSON parse**: Razorpay `HMAC-SHA256(body, secret)` hex, Stripe `HMAC-SHA256(t.body, secret)` with `t=` freshness 300s, bounded JSON `event_id/event_type/provider_order_id/amount/currency/occurred_at` no extra fields, amount binding, transition checks `created→authorized→captured→settled→refunded/failed`, idempotency via `operation_key` + `event_id` event hash, duplicate event_id with different body or different operation_key fails closed, `get`/`list` owner-scoped, `reconcile` local-only), `dashboard_metrics` `provider_orders/events` `sandbox_only`, `state_recovery_comparison` `compare_snapshot` `provider_events`, `tracker 31-33` (create/list/webhook).
- **Labels:** `payment_observations` are `unverified` placeholders (order→invoice snapshot), `financial_assertions` are `unverified` stages, `sandbox_*`/`provider_*` never revenue unless provider `settled` verified. Dashboard labels manual verification separately from provider/sandbox records; provider settlement remains unverified for live-money purposes.

## Ledger concepts (separate, never inferred)

1. Proposed (order draft)
2. Quoted (order draft)
3. Contracted (financial_assertion `contract_asserted` – unverified)
4. Invoiced (invoice draft, not issued) + `invoices` v20 `draft→issued→paid/overdue/void` PDF stub
5. Payment initiated (`initiated_asserted` – unverified)
6. **Manual payment verification annotation – LIMITED** (a settled manual record may be explicitly marked verified by an operator; this is not provider or bank verification)
7. **Payment settled (provider) – SANDBOX ONLY** (`payment_provider_orders` `settled` is HMAC sandbox, not bank settlement)
8. Refunds – SANDBOX ONLY (`refunded` via webhook `refunded` idempotent, not bank refund)
9. Expenses – `ledger_entries` `expense` unverified
10. Fees – `ledger_entries` `fee` unverified
11. **Net realized = settled − refunds − fees − costs – SANDBOX ARITHMETIC** (`ledger_11.net_realized_synthetic` is synthetic, not verified)

Manual settled records may carry an operator verification annotation, but this is not independent settlement evidence. Provider-backed records remain sandbox-only and do not become verified income. Any live provider treatment remains blocked by M11.

## Future provider (Razorpay candidate, per `DECISIONS.md`)

- **Selected but not verified:** Eligibility, KYC, sandbox access, credentials all `NOT STARTED` (blocked, M9 live).
- **Implemented sandbox (M9 v21):** `PaymentProviders` `ProviderConfig` `provider=razorpay|stripe` `mode=sandbox` `webhook_secret` 32..128, `create_order` local, `handle_webhook` raw-body HMAC before JSON parse (Razorpay hex, Stripe `t=,v1=`), reject extra/duplicate fields, amount/currency strictly equals `payment_provider_orders`, idempotency via `operation_key` + `event_id` `raw_body_hash`, replay 300s, transition `created→authorized→captured→settled→refunded/failed`, `compare_snapshot` `provider_events`, `dashboard` `provider_orders` `sandbox_only`, `reconcile` local-only.
- **Required before any live (M11 gate):** Real Razorpay/Stripe webhook secret verified against live provider, `live` mode enablement after explicit approval, settlement polling, bank reconciliation, `PAYMENTS.md` checklist signed, `live` transactions still blocked.

## Gate (M11) — expanded Phase 11 production readiness checklist

No live mode without all 11 checks (see `PRODUCTION_READINESS.md`):

1. Complete security review.
2. Complete webhook verification tests.
3. Verify reconciliation.
4. Verify duplicate event handling.
5. Verify refunds.
6. Verify failed payments.
7. Verify settlement records.
8. Verify audit trails.
9. Verify backups.
10. Verify error recovery.
11. Verify configuration separation.

Live remains blocked in code (`ProviderConfig` `mode=live` raises) and in `ai_earn/production_readiness.evaluate()` (`production_ready:false`, `live_blocked:true`). Checklist signing + explicit approval + code gate update are required. Live transactions remain unauthorized.

Automated verification: `python -c "from ai_earn.production_readiness import evaluate; import json,os; print(json.dumps(evaluate(os.environ), indent=2))"` and `python -m unittest tests.test_production_readiness -v`. Human sign-off table in `PRODUCTION_READINESS.md` must be completed.

Preserve warning: “Never mark a payment as received based only on a client-side success screen or unverified placeholder.”
