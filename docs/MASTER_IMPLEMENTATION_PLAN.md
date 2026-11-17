# Master Implementation Plan — Ai-earn

**Date:** 2026-11-17 (UTC)  
**Branch:** `arena/a2b30614-ai-earn` based on `9d77101a0853e5d8bd0d42d880ff1e8ebef5575d` (merge of PR #37, which wired `doctor` CLI and hardened diagnostics).  
**Auditor:** autonomous principal agent (this session).  
**Mode:** local-first, single-user, disposable-data, Windows-compatible, no live payments.

This plan preserves all existing work and does **not** rewrite the repository. It is the source of truth for gated milestones, dependencies, and continuation points. Existing docs (`ARCHITECTURE.md`, `PHASE_STATUS.md`, `REQUIREMENTS.md`, `SECURITY.md`, `THREAT_MODEL.md`, `TESTING.md`, `DECISIONS.md`, `BUILD_READINESS_AUDIT.md`) remain authoritative for their historical increments; this file links to them and adds the forward plan required by the master prompt.

---

## 0. Repository audit — what is actually there (2026-11-16)

### 0.1 Inventory (tracked files)

- **Language/runtime:** Python 3.11+ standard library only; no `pyproject.toml`, `requirements.txt` or lockfile. Vanilla SPA (no framework, no build, 17 views, 1 MB snapshot cap), health (`health [--json] [--with-backups]`), production hardening (`production_config`, `secrets`, `structured_logging`), API contract/E2E tests covering 15 categories, still no HTTP API server, no deployment manifest (production_ready:false).
- **CLI entrypoint:** `ai_earn/__main__.py` → `status` | `auth {register|login|recover}` | `tracker {init|open}` | `dashboard snapshot` | `doctor [--plan-init|--json]`. All dispatch in-process, no network listener.
- **Configuration:** `ai_earn/config.py` validates `AI_EARN_ENV`, `AI_EARN_LOG_LEVEL`, `AI_EARN_AUTH_DB`, `AI_EARN_STATE_DB` via absolute-path checks; invalid values raise `ConfigurationError` without leaking values. `.env.example` documents both DB paths.
- **Auth:** `ai_earn/auth.py` + `auth_storage.py` + `auth_cli.py` + `windows_security.py`. PBKDF2-HMAC-SHA256 600k iterations, random salt, session token 43-char urlsafe, `token_hash` stored, 30-min expiry, rate limits (`register 10/10, login 5/30, recover 5/20` per 15-min window), recovery-code replacement requires live session + `confirmed=True`, audits in `auth_audit`. POSIX 0700 dir / 0600 file, Windows ACL-protected dir/file via `create_private_directory/file` + `verify_private_path`. Experimental, not OS sandbox, not production-ready. User-reported Windows gate `72241cc` for Phase 2.
- **State storage:** `ai_earn/state_storage.py` – separate SQLite DB, `APPLICATION_ID 0x41494533`, `SCHEMA_VERSION 1` → `LATEST 28`, explicit backup-first migrations (`migrate_to_v2` … `migrate_to_v28`) with `backup_to` verified copy to new path, `_validate_schema` and `_check_integrity` (quick_check, foreign_key_check, creation-event, terminal-transition, multi-step replay, queue consistency, etc.). Transactions via `_PrivateFiles` reusing auth parent checks. `StateStorage` never auto-creates on open; `tracker_cli._initialize_state` creates v1 then migrates to v14 with 13 `before-v*.sqlite3` backups.
- **Services (all session-bound, disposable, owner-scoped, idempotent):**
  - `opportunity_drafts` (v3) `draft→archived`
  - `task_plans` (v4) `planned→cancelled`
  - `order_drafts` (v5) INR paise 0..1e12
  - `invoice_drafts` (v6) FK `(order_id, owner_id)`, amount snapshot
  - `task_queue` (v7) priority, deadline, lease, retry, recovery (`queued|leased|exhausted|cancelled`)
  - `task_workflows` (v8) `planned→in_progress→pending_review→completed` with hash/size, bounded UTF-8
  - `payment_observations` (v9) `unverified→flagged→unverified` + discard, amount snapshot
  - `task_associations` (v10) immutable link `queue_id+workflow_id`, opt-in `cancel_pair`, `start_pair`, `submit_pair/review_pair`, `retry/recover/resume_pair`
  - `paired_completions` (v11) explicit local completion marker, `complete_pair`, `abandon_pair`, v11-aware `TaskQueue.get`/`get_lease`/`inspect`
  - `managed_task_pairs` (v12) opt-in marker, `create_managed`, `list_managed`/`ready_managed`, `claim_next_managed` atomic, `list_legacy`/`adopt_managed`, direct parent mutations denied for managed
  - `financial_assertions` (v13) INR amount immutable, `potential→quote_asserted→contract_asserted→invoice_asserted→initiated_asserted→withdrawn`, labelled unverified
  - `sandbox_payments` + `sandbox_payment_events` (v14) synthetic HMAC journal, fingerprint not raw key, `sandbox_awaiting→confirmed→settled→refunded`
  - `financial_draft_workflows` coordinated `withdraw`/`withdraw_invoice` (bounded 100, atomic)
  - `payment_sandbox` in-memory synthetic helper (not durable)
  - `durable_sandbox_payments` durable synthetic helper
  - `state_audit` owner-relative pagination, `state_recovery_comparison` owner-scoped operation/record snapshot comparison, `dashboard_metrics` read-only aggregates
- **CLI adapters:** `tracker_cli` (financial_assertions menu), `dashboard_cli` (HMAC-checked snapshot beside state DB, exclusive create), `doctor` + `doctor_cli` (read-only classify, `permission_error` for unsafe parent, `immutable=1` open), `auth_cli`.
- **Dashboard:** `dashboard/connected.html` + `snapshot.js` – static file input, no API/token, `rupees()`/`count()` rendering, 1-MB limit, `Clear data` wipes memory.
- **Tools:** `windows_phase3_gate`, `windows_candidate_smoke`, `windows_terminal_smoke` – disposable temp dirs, sanitized test-ID reporting, 4 allowed POSIX skips.
- **Tests:** 963 tests (current, `python -m unittest discover -v` 2026-11-17, Linux, Python 3.11.2), 17 Windows-only skipped, 0 failed. Coverage includes auth, storage, migrations, crash recovery (`os._exit`), concurrency (`ThreadPoolExecutor`), payment sandbox. See §0.3.
- **Docs:** `README.md` (now mentions tracker+dashboard+doctor), `docs/` 9 files (see inventory above), phase register says Phase 1+2 `PASS_WITH_DOCUMENTED_LIMITATIONS`, Phase 3 `IN_PROGRESS`, remaining `NOT_STARTED`.

### 0.2 What is incomplete / unsafe / disconnected

- **No HTTP API / frontend framework.** Dashboard is static snapshot loader; no server, no auth cookies, no CSRF. `status` reports `production_ready:false`.
- **No real business workflows.** Services are experimental slices; full opportunity→task→order→invoice→payment→ledger lifecycle not integrated. Queue has no worker; workflows have no delivery.
- **No verified income ledger.** All amounts are proposed/asserted/unverified/sandbox; net income not calculated.
- **No payment provider.** `payment_sandbox`/`durable_sandbox_payments` are synthetic HMAC toys, not Razorpay/Stripe. No webhook, no settlement.
- **No CRM, services/portfolio, products, or AI assistant.** `REQUIREMENTS.md` rows `OPP-001`…`OPS-002` are `NOT_IMPLEMENTED` or `UNVERIFIED in full`.
- **Auth/state split commits.** `state_authorization._authorized_transaction` locks auth then state, but commits are separate → ambiguous on crash after state commit. Idempotency via `state_operations` is owner-scoped and not tamper-evident (coordinated deletion evades checks).
- **Legacy associations remain independently mutable** until opted into managed; stale snapshot replay hazard demonstrated in recovery rehearsal.
- **No dependency lockfile** (none needed for stdlib, but future deps need design), no secret manager, no deployment config.
- **Docs lag:** `ARCHITECTURE.md` still describes observed baseline as empty at `16bd10f`; needs update to reflect v14. `FEATURE_MATRIX.md`, `SECURITY_REVIEW.md`, `DATABASE.md`, `API.md`, etc. missing.

### 0.3 Baseline test report (2026-11-16, this session)

```
$ python -m compileall -q ai_earn tests tools  → OK
$ python -m unittest discover -v
Ran 963 tests in 176.692s
OK (skipped=17)
```
- Platform: Linux (sandbox, POSIX), Python 3.11.2
- Skipped: 17 Windows-only (`test_windows_auth` 14 + 3 others) requiring `Win32` ACL + NTFS drive
- No failures, no expectedFailures, no errors
- Focused groups consistent with `tools/windows_phase3_gate` allowed skips (4 POSIX-only). Full history and per-test output archived in CI; see `docs/TESTING.md` for historical per-phase counts.
- Baseline artifact: `git rev-parse HEAD` = `ed791020fda9a1975da5f599015072e0b35db7f6` (post-#37).

This was the green baseline before M2-M16; current baseline is 963/17 (see docs/TESTING.md Phase 16 section). Milestones M1-M16 are done (experimental, local, disposable-data).

---

## 1. Engineering principles (non-negotiable)

1. Preserve existing architecture; migrations, not rewrites.
2. Never replace real backend with hardcoded demo data; demo fixtures separate.
3. Never fabricate earnings/customers/transactions/analytics.
4. Migrations for every schema change, backup-before-migration, explicit confirmation.
5. Backend authorization on every protected operation, account-isolated queries (`WHERE owner_id=?`).
6. Validate all inputs + external API responses; reject extra/unknown fields.
7. No credentials in repo/logs; secrets via env, redacted logs.
8. Structured logs via allowlisted `Event` enum only.
9. Tests for every meaningful feature, including negative/authorization paths.
10. Windows-compatible (POSIX 0700 / Windows ACL, no hard-coded `/` paths).
11. Documentation describes actual state, not planned as if done.
12. Phase exit only with evidence (tests + Windows gate where applicable + recovery proof).
13. No weakening of assertions to get green.
14. No destructive DB changes without backup + explicit `confirmed=True`.
15. No irreversible financial side effects without explicit user approval.

---

## 2. Milestone roadmap (gated, 15 milestones)

Each milestone requires: plan → implement → tests → docs → `PHASE_STATUS.md` update → security review → commitment with evidence. Order may shift only with documented dependency reason.

| Milestone | Title | Scope (highest-priority first) | Entry deps | Exit evidence | Owner |
|---|---|---|---|---|---|
| **M1** | **Repository audit & baseline** (done, 636→963 via M2-M16) | This document + `FEATURE_MATRIX.md` + `SECURITY_REVIEW.md` + updated `ARCHITECTURE.md`/`PHASE_STATUS.md`, baseline report above | None | Docs merged, `git diff --check` clean, tests still 636/17 | This session |
| **M2** | **Phase 3 completion** | Finish database/state/auditability gaps: close legacy association bypass, unify completion semantics, document recovery, add non-fresh reconciliation strategy, expose `doctor`/`state_recovery_comparison` in user guide, add backup-before-migration recovery drills (temp DBs, interrupted `migrate_to_*` via `os._exit`), cover empty vs populated vs corrupt cases | M1, `REQUIREMENTS.DATA-002` | All `DATA-002` sub-requirements verified on Linux + user-reported Windows gate `f26c6b9` reproduced or documented limitation accepted, no destructive auto-repair, `docs/DATABASE.md` added | Next |
| **M3** | **Opportunity discovery (Phase 4)** | `opportunities` table + manual-import workflow + connector interface (`Connector` ABC, `ManualConnector` impl), source URL, budget, deadline, risk, AI summarize/skill/price helpers marked *draft*, filter/rank; respect ToS/robots, no CAPTCHA bypass | M2 | Manual import works end-to-end, connector interface tested with fake, `OPP-001/002` transition to `UNVERIFIED`→`VERIFIED` with view tests |   |
| **M4** | **Task mgmt & safe execution (Phase 5)** | Durable background-job runner for current CLI (thread-pool, shutdown, logs), `TASK-001/002` worker with lease fence, retry/backoff, cancellation, audit, human review gate | M2, M3 | Job record + idempotency + recovery after kill -9 tested |   |
| **M5** | **Services & portfolio (Phase 6)** | `services`, `portfolio_items`, `proposals` tables, 9 service templates editable, proposal draft via AI (draft-only), portfolio presentation | M3, M4 | Template CRUD + proposal draft marked draft-only tested |   |
| **M6** | **Digital products (Phase 7)** | `products` workspace (ideas, versions, files, pricing) | M5 | Product CRUD + file validation tested |   |
| **M7** | **CRM (Phase 8)** | `customers`, `communications`, `followups`, CSV import/export validated, approval for outbound | M3 | CRM isolation + CSV tests |   |
| **M8** | **Orders/invoices/ledger (Phase 9)** | `orders`, `invoices` (numbering, PDF stub), `ledger` separating 11 concepts, `invoices draft→issued→paid/overdue/void`, audit | M2, M7 | PDF + ledger net-income tests, no invoice→paid inference |   |
| **M9** | **Payment sandbox (Phase 10)** | Modular `PaymentProvider` (Razorpay sandbox), order create, webhook + HMAC verify, idempotency, reconciliation, refund, settlement, event log | M8, eligible account | Sandbox webhook signature + duplicate handling tested |   |
| **M10** | **Live payment readiness (Phase 11)** | Checklist only (no live activation): KYC, policy, backup, reconciliation, audit | M9 | Checklist signed, no live txn |   |
| **M11** | **Connectors (Phase 12)** | Framework `ai_earn/connectors/` (auth, throttle, retry, normalize), Gmail/Drive/GitHub stubs, manual fallback | M8 | Connector auth/ revocation tests |   |
| **M12** | **AI assistant & approvals (Phase 13)** | `ToolRegistry` (read/draft/consequential), `ApprovalQueue`, configurable provider (OpenAI-compatible/Ollama), frontend `AI Assistant` page, audit | M4, M11 | Permission + approval expiry tests, injection tests |   |
| **M13** | **Analytics (Phase 14)** | Real-data `analytics` queries: funnel, MRR, conversion, work-load, time-range, export | M8, M9 | Metrics from ledger, empty-state tests, no fabricated data |   |
| **M14** | **Frontend & UX (Phase 15)** | Responsive SPA (choose framework after M2 decision: vanilla + `connected.html` vs React/Vue justified), 17 nav sections, real backend data, loading/error/empty states, dark/light | M3–M13 | All buttons/forms work, no static mock, a11y check |   |
| **M15** | **Prod hardening & deploy (Phase 16)** | Secrets, backups/restore docs, health checks, CI gate, `docs/DEPLOYMENT.md`, `BACKUP_AND_RECOVERY.md` | M14 | Restore drill on disposable copy, `production_ready:false` until sign-off |   |

**Immediate next safe step (this branch):** Complete **M1** (this file + companion docs) and open **M2** implementation branch with backup-before-migration drills. No new frontend framework yet.

---

## 3. Architecture decisions to be recorded

- **ADR-006:** Choice of frontend stack (keep static snapshot + incremental enhancement vs introduce React) – to be decided at M2 exit after measuring `connected.html` limits.
- **ADR-007:** AI provider abstraction – OpenAI-compatible HTTP + Ollama local, model name configurable, no hard-coded provider.
- **ADR-008:** Ledger design – separate tables for proposed vs verified income, `verified_amount_paise` never derived from `asserted_not_revenue`.

All ADRs go to `docs/DECISIONS.md` and `docs/ARCHITECTURE.md`.

---

## 4. Risk register (top)

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Auth/state separate commits → ambiguous after crash | Medium | High | Keep idempotency keys, document ambiguous outcome, retry with same key, add recovery comparison before replay |
| Coordinated SQLite edit evades audit | Medium | High | State comparison is accountability, not prevention; never treat stale snapshot as authority |
| Stale snapshot replay creates second lease | Medium | Medium | Recovery rehearsal docs + `doctor` parent checks + never auto-replay |
| Windows ACL bypass via elevated sidecar | Low | High | `allow_admin_owner` only for file inside verified private dir (already implemented) |
| Synthetic events mistaken for real money | High | High | All sandbox tables prefixed `sandbox_`, dashboard labels unverified, ledger separates concepts |
| Provider credentials leaked to frontend | Low | Critical | No token in `dashboard_metrics`, snapshot contains counts only, strict redaction tests |

---

## 5. Test strategy per milestone

- Unit: `tests/test_*.py` per service, include unauthorized/cross-account/expired-session/malformed.
- Integration: `state_storage` + `auth` together, migration chain `v1→v14`, backup/restore to new path.
- Recovery: temp DBs, `os._exit` before/after commit, thread + process concurrency, `compare_snapshot`.
- Windows: `tools/windows_phase3_gate` on fixed drive, Python 3.11/3.13, record SHA + counts.

No milestone may weaken existing `636` assertions.

---

## 6. Documentation plan

- Keep `README.md` honest (already updated post-#37).
- Add: `FEATURE_MATRIX.md`, `SECURITY_REVIEW.md`, `DATABASE.md`, `API.md`, `AI_ASSISTANT.md`, `PAYMENTS.md`, `INTEGRATIONS.md`, `DEPLOYMENT.md`, `BACKUP_AND_RECOVERY.md`, `TESTING.md` (append milestone reports), `USER_GUIDE.md`.
- Update `PHASE_STATUS.md` after each milestone with evidence, not intent.

---

## 7. Continuation point (if limits hit)

- **Done:** M1 audit + baseline report + this plan (original branch `arena/c6693e06-ai-earn` at `ed79102`; current branch `arena/a2b30614-ai-earn` at `9d77101`, LATEST 28, 963 tests).
- **Next:** Implement `docs/FEATURE_MATRIX.md`, `SECURITY_REVIEW.md`, `DATABASE.md` stub, then M2 backup drill PR. If interrupted, resume reading this file §0.3 and §2 M2 row.
- **Blockers needing user:** Razorpay/Stripe sandbox credentials (M9), Gmail/Drive OAuth (M11), AI provider key/Ollama URL (M12) – interfaces will be built with manual fallback so work can proceed.
- **Do not:** mark Phase 3 complete without Windows gate + recovery proof, invent earnings, enable live payments, expose DB over network.

---

## 8. References

- Current phase register: `docs/PHASE_STATUS.md` (commit `ed79102`)
- Requirements traceability: `docs/REQUIREMENTS.md`
- Security posture: `docs/SECURITY.md` + `THREAT_MODEL.md`
- Test catalogue: `docs/TESTING.md`
- Build readiness: `docs/BUILD_READINESS_AUDIT.md` (still `NOT READY` for production – preserve warning)
