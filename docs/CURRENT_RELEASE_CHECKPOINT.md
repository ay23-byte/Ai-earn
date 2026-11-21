# Current Release Checkpoint

**Checkpoint date:** 2026-11-21  
**Canonical code baseline:** main at PR #60 merge commit 563bc1fc3676cfac9891e89c1969ffa6cd63dbf8  
**State schema:** v30 (LATEST_SCHEMA_VERSION = 30)  
**Purpose:** current evidence-based release status. Historical phase logs remain historical and are not silently rewritten.

## Product boundary

Ai-earn is a local-first freelance workflow and business-management application. The repository includes opportunity intake, services and portfolio, proposals, customer CRM, customer-linked projects and tasks, orders, invoices, finance records, sandbox payment foundations, AI drafting with approval controls, analytics, local authentication, workspace API/dashboard components, and backup/recovery tooling.

This is not a production hosted SaaS release. Operator acceptance on the supported Windows environment and an independent security review remain outstanding. The application does not guarantee income or independently obtain paying clients.

## Merged workflow increment

PR #59, **accepted proposal → customer order linking**, is merged into main at 2ae55c4aef0c09128faa0f5ab3198e4aab71d7a3.

The integrated business-flow regression test now covers:
- owner registration and tenant isolation;
- opportunity vetting and pursuit;
- service publication and customer/project creation;
- customer notes and requirements;
- project task transitions and execution history;
- draft-proposal rejection for order conversion;
- accepted customer-linked proposal conversion into an order;
- inherited order title/amount/customer/proposal relationship and duplicate rejection;
- invoice creation and issuance;
- sandbox payment creation with invoice and order links, remaining initiated and unverified.

This is a disposable SQLite-backed automated test. It does not call an external payment provider, prove external delivery, or establish real revenue.

## CI evidence

- PR #59 post-merge workflow run [#44895579177](https://github.com/ay23-byte/Ai-earn/actions/runs/44895579177) completed successfully on all four Python 3.11/3.13 Ubuntu/Windows jobs.
- PR #60 workflow run [#44895852566](https://github.com/ay23-byte/Ai-earn/actions/runs/44895852566) completed successfully on all four matrix jobs before merge.
- Main post-merge workflow run [#44898855718](https://github.com/ay23-byte/Ai-earn/actions/runs/44898855718) was still in progress when this checkpoint update was prepared; record its final result after completion.
- CI runs Ruff, mypy, compilation, security checks, dependency audit policy checks, health/doctor probes and python -m unittest discover -v.

## Release gates

| Gate | Evidence-based status | Required next action |
|---|---|---|
| Core opportunity/service/proposal/CRM/project/order/invoice workflow | Implemented and covered by the merged end-to-end API test | Complete operator smoke test using disposable data |
| Post-merge CI for PR #59 | Passed | Four Python 3.11/3.13 Ubuntu/Windows jobs succeeded in run #44895579177 |
| Windows operator acceptance | Not evidenced by GitHub CI alone | Run the Windows setup, workspace and backup/recovery smoke tests on the actual device |
| Backup and recovery | Code and disposable-database tests exist; real-device restore not evidenced | Perform a restore drill to a new private path; never overwrite the working DB |
| Accessibility | Automated checks and partial manual validation documented | Complete and record human browser review |
| Independent security review | Pending | Obtain scoped independent review and remediate findings |
| Hosted SaaS deployment | Not established | Make a separate product decision and design TLS, secrets, tenant isolation, monitoring, incident response and backup ownership |
| Live payments / verified settlement | Blocked by design | Keep live mode disabled until provider validation, reconciliation evidence, all required checks and accountable human sign-off |

## Payment and data safety

- Sandbox webhook signatures and local reconciliation are not proof of a real payment or bank settlement.
- Never mark income verified from a client-side success screen, manual claim or sandbox event.
- Do not store card data.
- Do not expose the local application or SQLite files directly to the public internet.
- Use disposable records for release rehearsals until operator acceptance and independent security review are complete.
- Preserve existing databases and backups; never use a destructive reset as a troubleshooting shortcut.

## Historical documentation policy

The phase register and older audits contain detailed snapshots from earlier branches. Keep those records available as history. A later amendment must identify its audited commit, evidence, test command/results, platform, skips and limitations. Do not copy historical test totals or infer that a historical phase passed on the current commit.

## Next execution sequence

1. Complete the Windows operator smoke test and backup/recovery drill on the actual device using disposable data.
2. Complete manual accessibility review and independent security testing.
3. Keep closing dashboard workflow gaps with owner-isolated API and browser integration tests.
4. Decide whether the product remains local-first or moves to a hosted SaaS track; treat hosted architecture as a separate security and operations project.
5. Evaluate live payments only after all provider, settlement, security, deployment and human approval gates are independently satisfied.

This checkpoint is not a release approval, security certification, deployment authorization or payment authorization.
