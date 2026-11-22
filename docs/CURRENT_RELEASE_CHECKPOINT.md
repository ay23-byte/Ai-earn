# Current Release Checkpoint

**Checkpoint date:** 2026-11-22
**Audited code baseline:** `main` at PR #74 merge commit `2d73d1c0e9d975886492fa835d943cd5d1351731` (includes PRs #73 and #74, plus the previously merged workspace, finance and safety work)
**State schema:** v30 (`LATEST_SCHEMA_VERSION = 30`)
**Release candidate:** not approved. Post-merge main CI run [#45001045311](https://github.com/ay23-byte/Ai-earn/actions/runs/45001045311) for the PR #74 merge commit was in progress when this checkpoint was prepared; do not treat it as passed until all four Python/OS matrix jobs complete successfully.

## Product boundary

Ai-earn is a local-first freelance workflow and small-business management application. It organizes manually sourced opportunities, services, proposals, customer CRM, customer-linked projects and tasks, orders, invoices, finance records, AI-assisted drafts, and local reporting.

It is not a hosted SaaS release, autonomous client-acquisition service, or income guarantee. A local task marked complete does not prove an external deliverable was sent or accepted. An issued invoice does not prove money was received.

## Recent merged increments

- **PR #63 — customer notes and requirements workspace:** merged at `b461a78fce896488413d82273439d7e6f8979515`. Adds owner-scoped customer notes/requirements to the live CRM detail workspace, with CSRF protection, idempotency keys, escaped text and responsive layout.
- **PR #64 — Windows operator acceptance and recovery drill:** merged at `fb6cd3494e34bb7a7ee50d57da3c2153a0e59d31`. Adds a repeatable procedure; it does not prove the actual device-level drill has been performed.
- **PR #65 — linked-record selectors:** closed without merge because its base became stale after PR #63 merged; superseded by PR #66.
- **PR #66 — linked-record selectors:** merged at `dab3a49513f03c02a02845f256f29ec4f0463656`. Adds owner-scoped live selectors for project customers, proposal services/customers, accepted customer-linked proposals when creating orders, and draft orders when creating invoices. PR CI passed all four Python/OS jobs in [run #44913475413](https://github.com/ay23-byte/Ai-earn/actions/runs/44913475413).
- **PR #67 — evidence-based release candidate checkpoint:** merged at `51545b9eae344783559519642b57b4f3fea821f3`. Updates the release checkpoint and records outstanding acceptance and release gates.
- **PR #68 — release checkpoint correction:** merged before PR #71; refreshed the release checkpoint after the PR #67 baseline.
- **PR #69 — payment safety and finance workflow E2E:** merged at `b4e10769f3b6d7c19eb10ab6f2c32466da0e4c52`. Adds server-side restrictions against creating pre-verified payments, prevents sandbox/provider records from being locally promoted to verified income, and adds finance workflow coverage.
- **PR #71 — HTTP workspace E2E and invoice hash persistence:** merged at `c7dec2719d199d87d7717dbe9888192d327407e5`. Adds HTTP-level workspace integration coverage and JSON-safe invoice hash persistence across restart.
- **PR #73 — release and architecture documentation refresh:** merged at `a7d178385e93798d5c54d94f5ba7fe5f8df7f9d2`. Updates the schema-v30 release baseline and clarifies that human acceptance and release gates remain open.
- **PR #74 — local Ollama draft provider:** merged at `2d73d1c0e9d975886492fa835d943cd5d1351731`. Adds authenticated owner-scoped local Ollama model configuration and draft inference, loopback-only HTTP endpoint validation, bounded non-streaming requests/responses, usage accounting, dashboard controls and tests. Model output remains `draft_unverified`; hosted providers and consequential actions remain disabled.

## Current CI evidence

- PR #66 CI [run #44913475413](https://github.com/ay23-byte/Ai-earn/actions/runs/44913475413): passed Python 3.11 and 3.13 on Ubuntu and Windows.
- PR #69 CI [run #44952173539](https://github.com/ay23-byte/Ai-earn/actions/runs/44952173539): passed all four Python/OS matrix jobs on the PR head. The separate post-merge `main` run remains tracked above.
- PR #67 CI [run #44929051309](https://github.com/ay23-byte/Ai-earn/actions/runs/44929051309): passed.
- Main post-merge CI [run #44956929296](https://github.com/ay23-byte/Ai-earn/actions/runs/44956929296) for commit `b4e10769f3b6d7c19eb10ab6f2c32466da0e4c52`: all four jobs passed (Python 3.11 and 3.13 on Ubuntu and Windows); this is historical evidence for the pre-Ollama baseline.
- PR #73 CI [run #44960154577](https://github.com/ay23-byte/Ai-earn/actions/runs/44960154577): passed on the documentation PR head.
- PR #74 CI [run #44999251252](https://github.com/ay23-byte/Ai-earn/actions/runs/44999251252): completed successfully on the PR head. Post-merge main CI [run #45001045311](https://github.com/ay23-byte/Ai-earn/actions/runs/45001045311) was still in progress at checkpoint time; verify its final result before treating main as green.

Required matrix: Python 3.11 and 3.13 on Ubuntu and Windows, including lint/type/compile/security/health checks and the unit suite. Record exact results, skips and commit SHA after completion.

## Release gates

| Gate | Current evidence-based status | Required action |
|---|---|---|
| Core opportunity, service, proposal and CRM workflows | Implemented; owner-scoped API and business-flow tests exist | Complete local operator smoke test using disposable data |
| Customer notes and requirements | Merged in PR #63 | Exercise create/read flows on the actual local dashboard |
| Linked-record selectors | Merged in PR #66; PR CI passed | Verify selector behavior in the actual Windows dashboard |
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

**Current decision: NOT APPROVED for public hosted SaaS or live-money use.** A local disposable-data release candidate can be considered only after the post-PR-74 main CI run is green and the Windows operator acceptance and restore drill are completed and recorded. Independent security and accessibility reviews remain separate gates.

This checkpoint is an evidence log, not a security certification, deployment authorization, payment authorization or income guarantee.
