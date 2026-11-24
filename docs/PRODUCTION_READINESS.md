# Production Readiness Checklist — Live Payments (Phase 11)

**Status:** `production_ready` reflects offline foundation readiness (`true` when all 11 automated checks PASS); **live remains blocked** (`live_blocked: true`, `live_ready: false` until M11). No live transactions have been enabled. This checklist must be **fully signed** before any live payment enablement. Live mode is blocked in code (`ProviderConfig` `mode=live` raises) and requires explicit human approval (M11) plus all automated verifications below. Do not process live money, store cards, or trust client success screens.

**Audited baseline:** `main` at PR #86 merge commit `425a1350131e898a5dfba8eb4c07f43db7f90401` — includes PRs #80–#86, the unified dashboard and AI workspace, and all previously merged workspace, finance, safety and local Ollama draft-provider work; state schema v30. These UI and integration changes do not change payment controls or satisfy a manual sign-off. This is a documentation baseline update, not a fresh independent security audit.
**Checklist reviewed:** 2026-11-25

PRs #62–#67 complete invoice/ledger, CRM detail, linked-record selector, and release-checkpoint increments; PR #68 corrected the release checkpoint; PR #69 added payment-verification safeguards and finance workflow coverage; PR #71 added HTTP-level workspace end-to-end coverage and JSON-safe invoice hash persistence; PR #73 refreshed release documentation; PR #74 added an optional loopback-only Ollama draft provider. None of these changes satisfy any manual sign-off gate. These do not satisfy any manual sign-off gate. Payment records must remain unverified unless an operator explicitly verifies a manually recorded payment after it is settled; sandbox and provider-backed records must never be promoted to verified income by a local UI action.
**Gate:** M11 (see `PAYMENTS.md`). This document IS the production readiness checklist.

## Checklist — all 11 must pass before live enablement

| # | Item | Verification (automated where noted) | Evidence | Owner | Signed |
|---|------|--------------------------------------|----------|-------|--------|
| 1 | **Complete security review.** | Automated: `docs/SECURITY_REVIEW.md` exists, non-empty, contains current threat scope, and `ai_earn/production_readiness.check_security_review()` passes. Manual: independent reviewer signs date + scope. | `docs/SECURITY_REVIEW.md`, `check_security_review()` | Security | ☐ |
| 2 | **Complete webhook verification tests.** | Automated: `tests/test_payment_providers.py` + `production_readiness.check_webhook_verification()` — Razorpay `X-Razorpay-Signature = HMAC-SHA256(raw_body, secret)` hex before JSON parse, Stripe `Stripe-Signature: t=..,v1=HMAC-SHA256(t.raw_body, secret)` with 300 s, constant-time compare, raw-body before parse, amount/provider_order_id binding, replay rejection. Run `python -m unittest tests.test_payment_providers -v` + `tests/test_production_readiness.py::Webhook`. | `tests/test_payment_providers.py`, `check_webhook_verification()` | Payments | ☐ |
| 3 | **Verify reconciliation.** | Automated: `check_reconciliation()` — create order → authorized → captured → settled, then `PaymentProviders.reconcile()` verifies audit replay, amount binding, `local_only`, no card columns, event counts match audit. Run `python -m unittest tests.test_payment_providers.TestReconciliation` + `test_production_readiness`. | `payment_providers.reconcile()`, `check_reconciliation()` | Payments | ☐ |
| 4 | **Verify duplicate event handling.** | Automated: `check_duplicate_handling()` — same `event_id`+`body_hash`+`operation_key` idempotent returns same status; same `event_id` with different body/amount/operation_key fails closed (`Conflicting webhook event`); out-of-order `created→settled` rejected. Tests: `test_payment_providers` duplicate/idempotency + `test_production_readiness`. | `payment_provider_events` unique `event_id`, `check_duplicate_handling()` | Payments | ☐ |
| 5 | **Verify refunds.** | Automated: `check_refunds()` — `captured`/`settled`→`refunded` allowed, `created`/`failed`→`refunded` denied, amount>order denied, owner-isolated, idempotent via `operation_key`, audited. Refunds are sandbox `refunded` until bank-verified. Tests: `tests/test_payment_providers` refund + `check_refunds()`. | `handle_refund()`, `handle_webhook` `refunded`, `check_refunds()` | Payments | ☐ |
| 6 | **Verify failed payments.** | Automated: `check_failed_payments()` — `created→failed`, `authorized→failed`, `captured→failed` allowed; `settled`→`failed` denied; failed is terminal except to `failed` idempotency. No client success inferred. Tests: `test_payment_providers` failed transitions + `check_failed_payments()`. | `TRANSITIONS` `failed`, `check_failed_payments()` | Payments | ☐ |
| 7 | **Verify settlement records.** | Automated: `check_settlement_records()` — `captured→settled` only, `get_settlement_status()` returns `settled` only when `settled` event exists (never inferred), owner-isolated, events logged. Tests: `test_payment_providers` settlement + `check_settlement_records()`. | `get_settlement_status()`, `handle_webhook` `settled`, `check_settlement_records()` | Payments | ☐ |
| 8 | **Verify audit trails.** | Automated: `check_audit_trails()` — every order has `provider_order_created` + exactly one audit row per `state_operations` entry; `_owned` replays `state_audit` and rejects inconsistent history; `state_audit` + `state_operations` counts match; `check_integrity` passes. No `DELETE`/`UPDATE` of audit in services. | `state_audit`, `state_operations`, `_check_audit_operation_balance`, `check_audit_trails()` | Data | ☐ |
| 9 | **Verify backups.** | Automated: `check_backups()` — `StateStorage` migration creates `before-v2..before-v{LATEST}` (29 for LATEST 30) via `backup_to` to new private path, never `REPLACE`; `health --with-backups` / `check_with_backups` reports `ok` with `count==expected` (29 backups for a fresh v30 database); restore drill on disposable copy (see `BACKUP_AND_RECOVERY.md` § drill). Tests: `test_state_migration`, `test_health.with_backups`, `check_backups()`. | `state_storage.backup_to`, `health.check_with_backups`, `check_backups()` | Ops | ☐ |
| 10 | **Verify error recovery.** | Automated: `check_error_recovery()` — `_check_sandbox_payment_integrity`/`_check_operation_results`/`_check_audit_operation_balance`, `StateRecoveryComparison.compare_snapshot` reports per-table differences, `os._exit` partial-write rollback tests, `backup_to` to new path never overwrites, `doctor --plan-init` read-only plan. Tests: `test_state_crash_recovery`, `test_state_recovery_comparison`, `test_recovery_drill`, `check_error_recovery()`. | `state_storage`, `state_recovery_comparison`, `doctor`, `check_error_recovery()` | Ops | ☐ |
| 11 | **Verify configuration separation.** | Automated: `check_configuration_separation()` — `ProviderConfig` `(provider,mode)` separate (sandbox vs live never share `webhook_secret`), live blocked without `docs/PRODUCTION_READINESS.md` sign-off, env vars `AI_EARN_ENV`/`AI_EARN_AUTH_DB`/`AI_EARN_STATE_DB` validated without logging, `.env` not loaded, secrets 32..128 bytes, never in repo/tests/logs/`dashboard_metrics`, no card storage (`PRAGMA table_info` has no `card`), provider/mode isolated in `handle_webhook`. Tests: `test_config`, `test_payment_providers.test_live_blocked`, `test_production_readiness`. | `ProviderConfig`, `config.Settings`, `check_configuration_separation()` | Security | ☐ |

## Manual Sign-off (required)

Live transactions remain **blocked** until **all 11** above are `PASS` **and** this section is signed:

- **Security reviewer:** ________________  Date: __________  Result: PASS / FAIL
- **Payments reviewer:** ________________  Date: __________  Result: PASS / FAIL
- **Ops reviewer:** ________________      Date: __________  Result: PASS / FAIL
- **Final approver (explicit M11):** ________________  Date: __________  Result: PASS / FAIL

Signing requires: independent verification of each evidence command above on a **disposable** private database (never reuse live DB), plus `python -m compileall -q ai_earn tests` OK, `python -m unittest discover` OK (Do not use a hard-coded historical test total; capture the current run's totals and skips for each OS/Python matrix leg), `git diff --check` clean, secret scan (`BEGIN PRIVATE KEY`, `sk-`) clean, and `ai_earn.production_readiness.evaluate()` reports `production_ready: true` offline (11 PASS) with `live_blocked: true` / `live_ready: false` until M11 form is stored out-of-repo and code gate is explicitly updated with the approval commit.

## How to verify (disposable)

```sh
python -m compileall -q ai_earn tests
python -m unittest discover -v  # record the exact current total, pass count and skips from this run; do not reuse historical totals
python -m ai_earn health --json --with-backups
python -m ai_earn doctor --json
# automated checklist (read-only, no network, no card, no live):
python -c "from ai_earn.production_readiness import evaluate; import json,os; print(json.dumps(evaluate(os.environ), indent=2))"
python -m unittest tests.test_production_readiness -v
python -m unittest tests.test_payment_providers -v
```

Live enablement is a **code change** (remove `ProviderConfig` live block after this checklist is signed and committed) plus independent deployment review; it is not flipped by this checklist file alone. Until M11 sign-off, `live_blocked` stays `true` (`live_ready` false) while offline `production_ready` is `true` when `health` `ok` and 11 checks PASS (`health`/`status` report offline readiness; `production_readiness.evaluate()` shows `production_ready:true`, `live_blocked:true`).

## References

- `PAYMENTS.md` Gate M11, `SECURITY.md`, `SECURITY_REVIEW.md`, `THREAT_MODEL.md`, `BACKUP_AND_RECOVERY.md`, `DEPLOYMENT.md`, `FEATURE_MATRIX.md`, `docs/PHASE_STATUS.md`
- Provider docs: Razorpay `X-Razorpay-Signature` HMAC-SHA256(raw_body) hex; Stripe `Stripe-Signature` `t=..,v1=HMAC-SHA256(t.raw_body)` hex + 300 s tolerance + multi-v1.

Preserve warning: “Never mark a payment as received based only on a client-side success screen or unverified placeholder.” — No live-money enablement is authorized by this checklist. The current branch/main baseline must be re-audited before any future M11 decision. The baseline SHA above identifies the latest reviewed documentation checkpoint, not independent reviewer approval.
