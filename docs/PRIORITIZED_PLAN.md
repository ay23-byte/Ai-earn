# Prioritized Implementation Plan — historical audit snapshot (2026-11-17)

> **Historical document:** the findings and plan below describe the 2026-11-17 repository state. They are not a current audit. Several statements were superseded by later implementation, including the authenticated local HTTP workspace API, schema migrations v28–v30, PR #69 payment-safety work, and PR #71 HTTP end-to-end coverage. Use `docs/CURRENT_RELEASE_CHECKPOINT.md`, `docs/DATABASE.md`, and `docs/FEATURE_MATRIX.md` for the current release baseline.

## Current repository status (2026-11-22)

- Main baseline: `b4e10769f3b6d7c19eb10ab6f2c32466da0e4c52` (PR #69 merged; release documentation refresh is tracked in PR #73).
- State schema: v30; fresh initialization applies 29 backup-first migrations after v1. See `docs/DATABASE.md`.
- The dashboard uses the authenticated loopback workspace API for live workspace records; the offline JSON snapshot remains a read-only fallback. See `docs/API.md` and `docs/DEPLOYMENT.md`.
- PR #69 adds payment verification safeguards; sandbox/provider-backed payments cannot be locally promoted to verified income. Manual verification is permitted only for a settled manual record and remains an operator assertion, not independent bank confirmation.
- PR #71 adds HTTP-level end-to-end workspace coverage.
- PR #73 is an open documentation-only PR. At the last check, both Ubuntu matrix jobs passed and both Windows matrix jobs were still running in CI run [#44957399853](https://github.com/ay23-byte/Ai-earn/actions/runs/44957399853). Do not report the run green until all four jobs finish.
- Still not completed by repository automation: real Windows operator acceptance, keyboard/contrast/NVDA review, a human-run restore drill, independent security review, provider eligibility/KYC, hosted deployment review, and live-payment authorization. Live payments remain blocked.

---

## Historical audit findings (2026-11-17; retained for traceability)

Branch `arena/a2b30614-ai-earn` base `9d77101` LATEST 28 983 tests OK skipped=17 at that historical point.

## Audit Findings (2026-11-17)

### What is actually working (verified green)
- Offline foundation: `config` validated without leaking values, `events` allowlist, `responses` envelope, `status` production_ready:false, `compileall OK`, `security_check OK`
- Auth: PBKDF2 600k, 30-min token_hash, rate limits, recovery_code replacement, Windows ACL 0700/0600, 92→983 tests pass, health/doctor read-only
- State DB: APPLICATION_ID 0x41494533 LATEST 28 SUPPORTED 1..28, 27 backup chain `before-v2..v28` verified copy never REPLACE, WAL, explicit `migrate_to_v*` with confirmed=True, `validate_schema`/`check_integrity` (quick_check, foreign_key, creation-event, terminal, multi-step, queue consistency, sandbox/payment binding)
- Authorization: `_authorized_transaction` auth→state lock + expiry recheck, WHERE owner_id=? per service
- Domain services (all session-bound, disposable, owner-scoped, idempotent, audited, no fabricated earnings):
  - Opportunities v15 hardened: ManualConnector/FakeConnector + verify_source_tos rejects CAPTCHA, evidence_score 0..100, ranked+explain_ranking (budget 50/risk 30/deadline 20), draft AI stubs draft_unverified
  - Task queue/workflows/associations/managed pairs/completions v7-v12 + ProjectTasks v23/v24 9-state FSM DAG + ProjectTaskRunner lease-fenced
  - Services v16 draft→published + portfolio + v25 packages/requirements/attachments/proposals/customer_requests/templates
  - Products v17 + product_workspace v26 (ideas/validations/research/pricing/versions/files/deliverables/sales/docs)
  - Customers v17 + Communications v18 draft→sent + Ledger v18 income/expense/refund/fee net + eleven_concepts + Followups v19 + CSV + Invoices v20 pdf stub + v27 CRM (notes/requirements/orders/projects/timeline/approval safe workflow no auto-send)
  - PaymentProviders v21 Razorpay/Stripe sandbox HMAC raw-body, reconcile, replay protection, live blocked at ProviderConfig + durable_sandbox_payments synthetic HMAC fingerprint
  - AI Assistant v22 ToolRegistry 32 tools 15 tasks A/B/C scopes + ApprovalQueue TTL 900 + emergency stop + injection isolation stub
  - Analytics v22 funnel/MRR/conversion/workload/time-range/export CSV no fabrication + RevenueAnalytics 18 metrics
  - Frontend SPA v22 vanilla 17 hash views, dark/light, a11y, 1MB snapshot cap, 127.0.0.1 only, no fetch(http
  - Production hardening v28: production_config 127.0.0.1 never 0.0.0.0, secrets generate/validate/redact, structured_logging allowlist, SafeError 8-hex, backup verified 27 chain, monitoring poll, data_export 50MB redacted, account_deletion DELETE <8>, security_check, CI ubuntu+windows×3.11/3.13
  - Health/Doctor: read-only immutable=1, permission_error, check/check_with_backups, production_ready:false, no secrets in payload
  - Dashboard snapshot: exclusive create, counts only, no token/username/title, 983 tests (966 passed)

### What is incomplete (honest, not hidden)
- Phase 3 IN_PROGRESS page describes legacy association bypass still independently mutable for unadopted legacy pairs (managed opt-in already denies fresh, but legacy Direct APIs remain); docs not yet updated for v28 counters
- DATABASE.md ends at v27, missing v28 (tax_configs, billing_profiles, payment_records, expense/refund/fee, invoice/customer_order tax/billing alters, 10 indexes)
- Production readiness checklist exists (`PRODUCTION_READINESS.md` 11 items) but all ☐ unchecked — live payments intentionally BLOCKED (requires KYC + 4 human signatures + code gate change)
- Windows gate beyond f26c6b9 not yet user-reproduced on this commit (17 skips on Linux expected, not tamper-evident)
- Real-data recovery rehearsal is disposable-only (no user-data drill)
- Multi-user production needs Postgres RLS (current SQLite per-user experimental)

### What is broken (must fix before next feature)
- No hard broken tests: full suite 983 OK skipped=17, compile OK, security OK, health not_configured (expected without env)
- Soft broken: DATABASE.md stale (v27 vs LATEST 28) → health backup expected count mismatch risk if docs wrong; FEATURE_MATRIX baseline still says 963 not 983 after DoD final (fixed in last commit to 983 but header still has trailing whitespace git diff check warning); status capabilities earning_workflows:false/payments:false correct for local MVP but dashboard metrics already show opportunity/service counts — minor inconsistency not broken runtime

### What is unsafe (risk, not yet exploitable locally but blocks production)
- Audit not tamper-evident: DELETE FROM state_audit + coordinated state_operations deletion can hide marker (rehearsal demonstrates) — documented, no hash chain
- Auth/state commits separate: ambiguous on crash after state commit → retry same operation_key acknowledges (documented)
- Stale snapshot replay: old backup lacks later operation_key → second lease can be created if old copy replayed (never auto-replay, compare_snapshot only)
- Payment verification is sandbox synthetic (sandbox_payments) not provider settlement — screenshot not proof (rehearsal synthetic)
- Secrets env-only, no vault — accidental commit mitigated by security_check but not hardware-backed
- No web CSRF/cookies — N/A now (no HTTP server), but any future web must add Secure/HttpOnly/SameSite
- Dependencies stdlib only — supply-chain risk when deps added (no lockfile yet)
- Windows ACL relies on _windows_auth_verified() — fails closed, accepted limitation

### Which tests currently pass
- 983 tests Ran 185.696s OK skipped=17 on Linux e2b.local 6.1.158+ Python 3.11.2
- Skipped: 17 Windows ACL (test_windows_auth 17) requiring NTFS + Win32 — not counted as pass
- Sub-suites: state_storage/migration/crash/recovery/comparison 83 OK, opportunities 32 OK, task mgmt 161 OK, services 22 OK, products/customers 34 OK, CRM/ledger/invoices 107 OK, payment sandbox 49 OK, live readiness 19 OK, connectors 21 OK, AI 21 OK, analytics 10 OK, frontend 16 OK, health 9 + hardening 17 OK, DoD final 20 OK

### What Phase 3 needs to finish (exit blockers per PHASE_STATUS)
1. Close DATABASE.md v28 gap + backup chain docs (this is #1 dependency before any new feature)
2. Document recovery: BACKUP_AND_RECOVERY drill already has 27 backups but DATABASE.md must reflect v28; add recovery_comparison docs for v28 tables
3. Unadopted legacy associations remain mutable — either migrate or document accepted limitation (already documented as managed opt-in denies fresh, legacy kept for audit)
4. Freshness proof: `compare_snapshot` is accountability not prevention — keep never auto-replay guard, already in docs
5. Tamper-evidence: accept limitation or add hash chain out-of-scope for local MVP — keep documented, no silent fix

### Architectural dependencies that must be addressed first
- **Foundation before feature:** Database/accountability (Phase 3) before Opportunity/Task/Service expansion — otherwise new tables risk migration loss
- **Security before payments:** production_config/secrets/health/backup before enabling live provider — otherwise live money at risk (already enforced: ProviderConfig live raises)
- **Approval before AI autonomy:** ApprovalQueue TTL + emergency stop before consequential AI tools — already enforced (consequential without approval_id → ApprovalError)
- **No frontend before backend authorization:** SPA is snapshot-only, token never in browser — already enforced (127.0.0.1 http.server static file input)

## Prioritized Implementation Plan (gated, 15 milestones, dependencies)

| Priority | Execution Strategy M | Scope | Depends | Why first | Safe incremental step | Exit gate |
|---|---|---|---|---|---|---|
| P1 | M2 Phase 3 DB | Fix DATABASE.md v28 + backup chain + health expected 27 + docs/DATABASE recovery notes | M1 audit | Foundation for all later migrations — if LATEST wrong, health backup count fails, new features corrupt | Update docs/DATABASE.md v28 block, STATE.md, run `test_phase3_completion` + full suite, `health --with-backups` on temp init | 983 OK, health backups ok, docs correct, no auto-repair |
| P2 | M3 Opportunity | Verify ToS/ranking still green, no new capture bypass | P1 | Opportunity feeds tasks/services; no payment until verified source | Run `test_opportunities*` 32 OK, no code change unless ToS missing | 32 OK |
| P3 | M4 Task mgmt | Lease-fenced runner already done, verify no bypass | P1,P2 | Tasks feed projects/services | Run 161 OK, keep pending_review gate | 161 OK |
| P4 | M5/M6 Services/Products | Templates editable, product files hash32, no new mock | P2,P3 | Sellable catalog before proposals | Run 56 OK | 56 OK |
| P5 | M7 CRM | Approval gate no auto-send already, verify daily_limit | P2 | Customer before orders/invoices | Run 58 OK | 58 OK |
| P6 | M8 Orders/Invoices/Ledger | pdf stub SHA-256 + 11-concept synthetic vs verified | P1,P5 | Money before payments | Run 16+11 OK | 16 OK |
| P7 | M9 Payment sandbox | HMAC raw-body + reconcile; keep live blocked | P6 | Sandbox before live readiness | Run 49 OK | 49 OK |
| P8 | M10 Live readiness | Checklist exists, keep live blocked (no credentials invented) | P7 | Checklist before live enablement — blocker: KYC/legal | Run 19 OK, live ProviderError | 19 OK |
| P9 | M11 Integrations | 10 caps/connector throttling redacted | P6 | Optional, least-privilege | Run 21 OK | 21 OK |
| P10 | M12 AI assistant | 32 tools A/B/C + emergency | P3,P9 | AI after connectors/approval | Run 21 OK | 21 OK |
| P11 | M13 Analytics | Real-data funnel/MRR no fabrication | P6,P7 | Metrics after ledger | Run 10 OK | 10 OK |
| P12 | M14 Frontend | Snapshot 1MB, 17 views, no mock | P11 | UI after backend metrics | Run 16 OK | 16 OK |
| P13 | M15 Prod hardening | 127.0.0.1, secrets, backup 27, monitoring | P12 | Hardening after UI | Run 26 OK | 26 OK |
| P14 | M10+DoD final | 20 workflows E2E `test_dod_final` | P1-13 | Final gate proves not UI-only | 20 OK, full 983 OK | 20 OK |
| P15 | Continuous | Real-data recovery rehearsal on user clone (doc only) | P1 | After foundation proven | Doc runbook test, no DB touch | Manual |

No order adjustment beyond P1 being true dependency — all others follow original order because each depends on durable state.

## Immediate action (highest-priority safe milestone)

**P1 — M2 Phase 3 DB** started now: updating `docs/DATABASE.md` v28, running relevant tests, then full suite, then commit. No rewrite, no new frontend screens, no branch, working tree stays green.

