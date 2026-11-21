# Current Release Checkpoint

**Checkpoint date:** 2026-11-21  
**Audited code baseline:** `main` at PR #63 merge commit `b461a78fce896488413d82273439d7e6f8979515`  
**State schema:** v30 (`LATEST_SCHEMA_VERSION = 30`)  
**Release candidate:** not yet cut; PR #66 is the final linked-record selector candidate and remains open/draft.

## Product boundary

Ai-earn is a local-first freelance workflow and small-business management application. It organizes manually sourced opportunities, services, proposals, customer CRM, customer-linked projects and tasks, orders, invoices, finance records, AI-assisted drafts, and local reporting.

It is not a hosted SaaS release, autonomous client-acquisition service, or income guarantee. A local task marked complete does not prove an external deliverable was sent or accepted. An issued invoice does not prove money was received.

## Recent merged increments

- **PR #63 — customer notes and requirements workspace:** merged at `b461a78fce896488413d82273439d7e6f8979515`. Adds owner-scoped customer notes/requirements to the live CRM detail workspace, with CSRF protection, idempotency keys, escaped text and responsive layout.
- **PR #64 — Windows operator acceptance and recovery drill:** merged at `fb6cd3494e34bb7a7ee50d57da3c2153a0e59d31`. Adds a repeatable procedure. The procedure being merged is not evidence that the actual device-level drill has been performed.
- **PR #65 — linked-record selectors:** closed without merge because its base became stale after PR #63 merged. It is superseded by PR #66.
- **PR #66 — linked-record selectors on current main:** open draft. Reapplies the selector work on top of PR #63 so projects, proposals, orders and invoices can choose existing owner-scoped records instead of requiring copied IDs. It has not yet passed its current CI run and is not merged.

## Current CI evidence

- Main post-PR-63 run [#44913093454](https://github.com/ay23-byte/Ai-earn/actions/runs/44913093454) was in progress when this checkpoint was written; do not treat it as passed.
- PR #66 run [#44913475413](https://github.com/ay23-byte/Ai-earn/actions/runs/44913475413) was queued when this checkpoint was written; do not treat it as passed.
- Earlier successful runs are historical evidence only and do not certify the current main or PR #66 head.

Required matrix: Python 3.11 and 3.13 on Ubuntu and Windows, including lint/type/compile/security/health checks and the unit suite. Record exact current results, skips and commit SHA after completion.

## Release gates

| Gate | Current evidence-based status | Required action |
|---|---|---|
| Core opportunity, service, proposal and CRM workflows | Implemented; owner-scoped API and business-flow tests exist | Complete local operator smoke test using disposable data |
| Customer notes and requirements | Merged in PR #63 | Exercise create/read flows on the actual local dashboard |
| Linked-record selectors | Pending PR #66 | Review diff and require all four CI matrix jobs to pass before merge |
| Dashboard/backend integration | Authenticated loopback API and live dashboard are implemented | Verify sign-in, live records, CRUD and logout on Windows |
| Windows operator acceptance | Not performed by this automation | Follow `docs/LOCAL_OPERATOR_ACCEPTANCE_DRILL.md`; record PASS/FAIL/NOT RUN |
| Backup and restore | Code and automated disposable-DB tests exist; actual device restore is unverified | Restore to a new private path; run health/doctor; never overwrite the working DB |
| Accessibility | Automated structure checks exist; human review is outstanding | Keyboard-only, contrast, focus and screen-reader review in a real browser |
| Independent security review | Pending | Obtain an independent scoped review and remediate findings |
| Hosted SaaS deployment | Not approved or established | Treat as a separate architecture, operations and security project |
| Live payments / verified settlement | **Blocked by design** | Keep disabled until all payment, security, deployment and accountable human sign-off gates are met |

## Operator acceptance sequence

Use a new disposable private directory and synthetic account. Do not use an existing personal database.

1. Follow the Windows-first setup in `README.md` and `docs/DEPLOYMENT.md`.
2. Run `python -m compileall -q ai_earn tests`.
3. Run `python -m unittest discover -v` and capture exact pass/fail/skip totals.
4. Run `python -m ai_earn health --json --with-backups` and `python -m ai_earn doctor --json`.
5. Start `python -m ai_earn serve` and verify it binds only to loopback.
6. Exercise opportunities, services, proposals, CRM notes/requirements, projects/tasks, accepted proposal to order, invoice issue, ledger and logout.
7. Exercise backup to a new private path, restore only to a new private path, and verify with health/doctor.
8. Complete keyboard, contrast, focus and screen-reader review.
9. Record evidence and limitations in `docs/LOCAL_OPERATOR_ACCEPTANCE_DRILL.md`.

Do not paste passwords, recovery codes, tokens, private database contents or customer data into issues or chat.

## Payment and data safety

- Sandbox/test events are not evidence of money received or bank settlement.
- Never mark income verified from a client-side success page, manual claim, screenshot or sandbox event.
- Do not store card data.
- Do not expose the local application or SQLite files directly to the public internet.
- Keep live payment mode blocked; this checkpoint grants no payment authorization.
- Preserve existing databases and backups; never use destructive resets as a troubleshooting shortcut.

## Release decision

**Current decision: NOT APPROVED for public hosted SaaS or live-money use.** A local disposable-data release candidate can be considered only after PR #66 is resolved, current main CI is green, and the Windows operator acceptance and restore drill are completed and recorded. Independent security and accessibility reviews remain separate gates.

This checkpoint is an evidence log, not a security certification, deployment authorization, payment authorization or income guarantee.
